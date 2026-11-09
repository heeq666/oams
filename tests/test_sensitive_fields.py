"""敏感字段登记表：`oams db init` 之后这张表就得是能用的。

脱敏的整条链路（哪些字段算敏感 → 谁能读 → 读时打码、逐条记审计）都从这张表出发，
所以它不能是空的。这张表在 `schema.sql` 里建了，可**建表不等于有数据**——2026-11-09
查过一次：表在、行数 0、代码里 `grep sensitive_field` 也是 0 处，等于一条从没通电的链路。

这里的用例只钉「种子」这一层，而且**走文档上的那条命令**（`oams db init`）在一个独立新库上
验，不是在本机那个「碰巧被人灌过」的开发库上验——那正是上一轮读权收敛踩过的坑：
用例造什么数据就只验什么数据。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

pytestmark = pytest.mark.db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.sensitive_fields import SENSITIVE_FIELDS, declared_keys  # noqa: E402


@pytest.fixture
def fresh(db_ready):
    """一条用例一个刚建出来的空库——`db init` 之外什么都没做过。

    不给 module 作用域：`test_db_init_is_idempotent_and_does_not_overwrite` 会故意把
    `identity_card` 的 label 改掉，共用一个库会让后面的用例看到别人改过的状态，
    那种红是测试自己串味，不是代码有问题。
    """

    name, url = suite._create_database(db_ready)
    try:
        yield url
    finally:
        suite._drop_database(db_ready, name)


def _rows(url: str) -> dict[str, dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        rows = conn.execute(
            """SELECT field_key, label, description, requires_explicit_grant
                 FROM oams.sensitive_field_registry"""
        ).fetchall()
    return {row["field_key"]: row for row in rows}


def test_db_init_seeds_the_registry(fresh):
    """建完表就得有六条登记——空表等于脱敏链路没通电。"""

    suite._cli(["db", "init"], fresh)

    rows = _rows(fresh)
    assert set(rows) == set(declared_keys()), (
        f"登记表应有 {sorted(declared_keys())}，实际 {sorted(rows)}"
    )
    for field_key in declared_keys():
        row = rows[field_key]
        assert row["label"], f"{field_key} 缺 label"
        assert row["description"], f"{field_key} 缺 description——它要能挡住「只想到身份证号、没想到证件影像」这种半覆盖"
        assert row["requires_explicit_grant"] is True, (
            f"{field_key} 必须要求单独授权：人事敏感字段不随「管理层」自动放开"
        )


def test_db_init_is_idempotent_and_does_not_overwrite(fresh):
    """重跑 `db init` 不加行、也不把管理员改过的说法改回出厂设置。"""

    suite._cli(["db", "init"], fresh)

    import psycopg

    with psycopg.connect(fresh, autocommit=True) as conn:
        conn.execute(
            "UPDATE oams.sensitive_field_registry SET label = '身份证件' WHERE field_key = 'identity_card'"
        )

    suite._cli(["db", "init"], fresh)

    rows = _rows(fresh)
    assert len(rows) == len(declared_keys()), "重跑 db init 不该多出登记行"
    assert rows["identity_card"]["label"] == "身份证件", (
        "重跑 db init 把管理员改过的 label 冲回了出厂值——那等于每次建库都覆盖别人的口径"
    )


def test_the_seed_rows_are_the_ones_the_hr_matrix_names(fresh):
    """六个字段与人事矩阵对得上：身份证、银行卡、薪资、合同、绩效、入职材料。"""

    suite._cli(["db", "init"], fresh)

    labels = {row["label"] for row in _rows(fresh).values()}
    assert labels == {"身份证", "银行卡", "薪资", "合同", "绩效", "入职材料"}
    assert [row["field_key"] for row in SENSITIVE_FIELDS] == list(declared_keys())


# ---------------------------------------------------------------------------
# 列分类：哪一列能出去，由宿主判
# ---------------------------------------------------------------------------

from oams.sensitive_fields import (  # noqa: E402
    MEMBER_READ_FIELDS,
    PERSONNEL_COLUMNS,
    PERSONNEL_TABLE,
    UnclassifiedPersonnelColumn,
    project_member_row,
    sensitive_keys_in,
)


def _columns(url: str) -> set[str]:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            """SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'oams' AND table_name = %s""",
            (PERSONNEL_TABLE,),
        ).fetchall()
    return {row[0] for row in rows}


def test_every_personnel_column_is_classified(fresh):
    """双向核对：库里的列必须有分类，分类里的列必须还在库里。

    只核一向是不够的——只核「列都有分类」，删掉一列后那条分类就成了幽灵；只核
    「分类都在库里」，新加的列就永远没人管。两个方向各一条断言，哪边漏了都红。
    """

    suite._cli(["db", "init"], fresh)

    actual = _columns(fresh)
    declared = set(PERSONNEL_COLUMNS)
    assert not (actual - declared), (
        f"这些列没有分类，闸门不知道该不该放它们出去：{sorted(actual - declared)}"
        "——加列的人必须在 sensitive_fields.PERSONNEL_COLUMNS 里判一次"
    )
    assert not (declared - actual), (
        f"分类里有已经不存在的列（列被删了、分类没跟上）：{sorted(declared - actual)}"
    )


def test_every_sensitive_class_is_registered(fresh):
    """分类里用到的敏感键必须**登记过**——这就是「声明了就得有用」那条闸门。

    登记表是「哪些字段算敏感」的唯一口径。分类里写一个没登记过的键，等于代码自己
    发明了一格敏感字段：管理员在设置里看不到它、也没法按公司口径关掉它。
    """

    suite._cli(["db", "init"], fresh)

    used = {cls for cls in PERSONNEL_COLUMNS.values() if cls not in ("public", "internal")}
    assert used <= set(declared_keys()), (
        f"这些敏感键没有登记：{sorted(used - set(declared_keys()))}"
    )
    assert used <= set(_rows(fresh)), (
        f"登记表里没有这些键（种子没写进去？）：{sorted(used - set(_rows(fresh)))}"
    )


def test_the_members_read_fields_are_public_or_sensitive():
    """成员列表返回的字段不许是 `internal`：内部字段连打码都不该出现。"""

    for field in MEMBER_READ_FIELDS:
        assert field in PERSONNEL_COLUMNS, f"{field} 没分类"
        assert PERSONNEL_COLUMNS[field] != "internal", (
            f"{field} 是内部字段，却被列进了返回字段"
        )


def test_project_member_row_masks_a_sensitive_column(monkeypatch):
    """函数级验掩码：直接喂一条带敏感键的行，不用为了喂闸门去造页面。

    今天的真实人员表里一列敏感字段都没有，所以这里临时给一个假列分类——它走的正是
    「以后加了敏感列」时的那条路径，不是另写一段。
    """

    monkeypatch.setitem(PERSONNEL_COLUMNS, "salary", "salary")
    row = {"member_id": "m-1", "display_name": "张三", "salary": 12000}

    masked = project_member_row(row)
    assert masked["salary"] == "***", masked
    assert masked["display_name"] == "张三"

    plain = project_member_row(row, readable=frozenset({"salary"}))
    assert plain["salary"] == 12000, plain


def test_project_member_row_drops_internal_columns():
    """查询多选了内部字段也不会漏出去：`token_version` 连键都不出现。"""

    projected = project_member_row(
        {"member_id": "m-1", "display_name": "张三", "token_version": 7, "created_at": "2026-11-09"}
    )
    assert "token_version" not in projected, projected
    assert "created_at" not in projected, projected
    assert projected == {"member_id": "m-1", "display_name": "张三"}


def test_project_member_row_refuses_an_unclassified_column():
    """没分类的列宁可报错也不出去——沉默地放行是这类闸门最坏的失败方式。"""

    with pytest.raises(UnclassifiedPersonnelColumn):
        project_member_row({"member_id": "m-1", "secret_note": "谁也没判过这一列"})


def test_sensitive_keys_in_ignores_public_and_internal():
    assert sensitive_keys_in(("member_id", "token_version")) == frozenset()
    with pytest.raises(UnclassifiedPersonnelColumn):
        sensitive_keys_in(("member_id", "nope"))


# ---------------------------------------------------------------------------
# 谁能读明文：判据在库里，不在代码里的出厂设置
# ---------------------------------------------------------------------------


def _member(url: str, employee_code: str) -> str:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return str(conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
            (employee_code,)).fetchone()["member_id"])


def _grant(url: str, member_id: str, resource: str, action: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO oams.permission_grants
                   (subject_type, member_id, resource, action, scope_type,
                    effect, valid_from, valid_until, reason, approved_by, approved_at)
               VALUES ('member', %s, %s, %s, 'all', 'allow',
                       now() - interval '1 day', now() + interval '30 days',
                       '测试用', %s, now())""",
            (member_id, resource, action, member_id),
        )


@pytest.fixture
def hr_world(fresh):
    """一个管理员 + 一条人事授权——验「谁能看明文」够用的最小世界。"""

    import psycopg

    suite._cli(["db", "init"], fresh)
    suite._cli(
        ["bootstrap-admin", "--name", "管理员", "--employee-code", "ADMIN001",
         "--org-code", "COMPANY", "--org-name", "公司"],
        fresh,
    )
    with psycopg.connect(fresh, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
               VALUES ('HR001', '李人事', 'active')""")
    return fresh


def test_a_grant_opens_the_sensitive_field_and_an_admin_role_does_not(hr_world):
    """管理员是运维角色：他的管理权**不**自动换成薪资的读权，要单独授。"""

    import psycopg
    from psycopg.rows import dict_row

    from oams.security import SENSITIVE_FIELD_RESOURCE, can_read_sensitive_field

    admin_id = _member(hr_world, "ADMIN001")
    hr_id = _member(hr_world, "HR001")
    with psycopg.connect(hr_world, row_factory=dict_row) as conn:
        # 管理员手里有一堆 manage 授权（bootstrap-admin 发的），但薪资仍然读不了
        assert can_read_sensitive_field(conn, admin_id, "salary") is False, (
            "管理员自动拿到了薪资读权——人事敏感字段不随「管理层」放开"
        )
        assert can_read_sensitive_field(conn, hr_id, "salary") is False

        _grant(hr_world, hr_id, SENSITIVE_FIELD_RESOURCE, "read")
        assert can_read_sensitive_field(conn, hr_id, "salary") is True

        # 没登记的键一律不可读：不是「无所谓」，是「没人判过」
        assert can_read_sensitive_field(conn, hr_id, "nickname") is False


def test_the_registry_row_decides_whether_a_grant_is_needed(hr_world):
    """登记行说「不必单独授权」，就按登记表放行——口径跟着库走，不跟着出厂设置走。"""

    import psycopg
    from psycopg.rows import dict_row

    from oams.security import can_read_sensitive_field

    hr_id = _member(hr_world, "HR001")
    with psycopg.connect(hr_world, autocommit=True) as conn:
        conn.execute(
            "UPDATE oams.sensitive_field_registry SET requires_explicit_grant = false "
            "WHERE field_key = 'performance'")
    with psycopg.connect(hr_world, row_factory=dict_row) as conn:
        assert can_read_sensitive_field(conn, hr_id, "performance") is True
        assert can_read_sensitive_field(conn, hr_id, "salary") is False


# ---------------------------------------------------------------------------
# 响应里不许出现内部字段：分类说「永不返回」，就得有接口级证据
# ---------------------------------------------------------------------------


@pytest.fixture
def served_members(db_ready):
    """一个只做过 `db init` + `bootstrap-admin` 的库，起真服务。

    为什么不用开发库：开发库里「碰巧」没有敏感列、也没有多余返回字段，
    验不出「响应里多出内部字段」。只有从零建起来、自己造成员的库，
    返回体的键集合才是这条闸门说了算。
    """

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            [
                "bootstrap-admin",
                "--name", "管理员",
                "--employee-code", "ADMIN001",
                "--org-code", "COMPANY",
                "--org-name", "公司",
            ],
            url,
        )
        with suite.serve(url) as base:
            yield {"url": url, "base": base}
    finally:
        suite._drop_database(db_ready, name)


def _call(base: str, method: str, path: str, token: str, payload: dict | None = None):
    import json as _json
    import urllib.error
    import urllib.request

    body = _json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=body, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, _json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, _json.loads(error.read().decode())


def _token_for(url: str, employee_code: str) -> str:
    from oams.security import issue_session

    return issue_session(_member(url, employee_code))[0]


def test_member_responses_never_carry_internal_columns(served_members):
    """建成员、改状态的返回体只许有公开列——`token_version` 连键都不出现。

    2026-11-09 的实情：这两处 `RETURNING` 里带着 `token_version`，连接用的是
    `dict_row`，整行原样序列化进响应体，分类里写的「内部字段永不返回」当场不成立。
    这条用例盯的是**接口实际返回了什么**，不是函数里写了什么。
    """

    token = _token_for(served_members["url"], "ADMIN001")
    status, created = _call(
        served_members["base"], "POST", "/v1/members", token,
        {"employee_code": "NEW001", "display_name": "新人"})
    assert status == 201, created
    assert set(created) == {
        "member_id", "employee_code", "display_name", "employment_status", "joined_at"
    }, created

    status, updated = _call(
        served_members["base"], "PATCH", f"/v1/members/{created['member_id']}/status", token,
        {"employment_status": "suspended", "reason": "验证响应体"})
    assert status == 200, updated
    assert set(updated) == {
        "member_id", "display_name", "employment_status", "departed_at"
    }, updated

    # 「重新启用」这条路也得走一遍：原来那个 activate_member 死代码删掉之后，
    # 它就是唯一的重新启用入口，不能只验「停用」那一半。
    status, reactivated = _call(
        served_members["base"], "PATCH", f"/v1/members/{created['member_id']}/status", token,
        {"employment_status": "active", "reason": "重新启用"})
    assert status == 200, reactivated
    assert reactivated["employment_status"] == "active", reactivated
    assert reactivated["departed_at"] is None, reactivated
    assert set(reactivated) == {
        "member_id", "display_name", "employment_status", "departed_at"
    }, reactivated


# ---------------------------------------------------------------------------
# 人员表的访问面：谁碰这张表，就得写清为什么可以
# ---------------------------------------------------------------------------
#
# 脱敏闸门只装在一个门上（`GET /v1/members` 的返回经 `project_member_row`）。
# 只要别处还能把整行取出来塞进响应，分类里写的「internal 永不返回」就是空话——
# 2026-11-09 就是这么发现两处 `RETURNING` 里带着 `token_version` 的：闸门装好了，
# 旁边那扇窗没关。其中 `activate_member` 是死代码（没人调），已连同它的用例一起删掉；
# 留着的 `update_member_status` 只返回公开列。
#
# 所以这里把「谁碰这张表」列成清单，由机器对账，不靠人记得住：
#   · 扫 `app/oams/**.py` 里所有**带限定表名** `oams.workforce_members` 的字符串常量
#     （限定名才认，是为了不把注释、docstring、`PERSONNEL_TABLE` 这种常量算进来）；
#   · 按「文件::函数」归并并数条数；
#   · 清单里没有的 → 红（新加一处没登记）；清单里有、代码里没有 → 红（删了没销账）；
#     条数对不上 → 红（在已有函数里又塞了一条查询）。
#
# 每一条都要写清「为什么可以」。写不出来的那一条，就是下一个泄漏点。
#
# 这道清单**看不出列的选择**：`token_version` 是不是被写进了某条显式列里，机器不知道，
# 只能靠人在上面每条理由里写清（2026-11-09 就是这么发现两处 RETURNING 带 token_version 的）。
# 全自动的那半截另有一条闸门——`test_personnel_queries_never_select_star`，它只管
# 「有没有 `SELECT *` / `别名.*`」，管不了「显式列挑错了」。两条合起来才够，别以为哪一条能单挑。

QUALIFIED_TABLE = "oams.workforce_members"

PERSONNEL_ACCESS_POINTS: dict[str, tuple[int, str]] = {
    # ---- API 层：只有 list_members 的返回能出站，且必须过 project_member_row ----
    "api.py::list_members": (2, "全公司 / 部门范围两条查询，返回前统一过 project_member_row（唯一读取口）"),
    "api.py::insert_member": (1, "INSERT RETURNING 五个公开列"),
    "api.py::update_member_status": (2, "显式四列 FOR UPDATE 取 before_state（与 UPDATE 的 RETURNING 同键）；UPDATE RETURNING 只取公开列"),
    "api.py::insert_assignment": (2, "读在职状态判能不能派岗；UPDATE 自增 token_version 作废旧会话，不返回行"),
    "api.py::end_assignment": (1, "UPDATE 自增 token_version 作废旧会话，不返回行"),
    "api.py::list_assignments": (1, "联表取 display_name 做展示"),
    "api.py::list_work_item_reports": (1, "联表取 display_name AS reported_by"),
    "api.py::_page_activity": (1, "审计流联表取 display_name 当 actor"),
    "api.py::exchange_app_token": (1, "只取 display_name，回填「这张票代表谁」"),
    "api.py::revoke_authorization": (1, "只取 display_name 记审计"),
    # ---- CLI：命令行只在终端里出现，但同样只取要用的列 ----
    "cli.py::bootstrap_admin": (1, "INSERT RETURNING member_id"),
    "cli.py::_member_id_from_code": (1, "按员工编号取 id，只回 id"),
    # ---- 协作室：姓名 / 员工编号做展示，不碰内部字段 ----
    "collab.py::<module>": (1, "_DIRECTORY_SQL 通讯录，只选公开列 + 岗位与部门名"),
    "collab.py::visible_dms": (1, "联表取对方姓名与员工编号"),
    "collab.py::thread_header": (1, "只取 display_name / employee_code 做会话头"),
    "collab.py::list_messages": (1, "联表取 display_name AS author_name"),
    "collab.py::post_message": (1, "只取 display_name 记消息作者名"),
    "plugin_cli.py::active_employee_codes": (1, "只取在职员工编号，做插件身份的候选"),
    # ---- 身份与会话：token_version 只用来比对，不出现在任何响应里 ----
    "security.py::issue_session": (1, "读在职状态与 token_version 校验并发会话，值只写进会话表"),
    "security.py::principal_for_token": (1, "票校验，token_version 只做比对"),
    "security.py::<module>": (2, "开发模式的取身份 SQL，token_version 只做会话比对，不进响应"),
    # ---- Web 层：服务端渲染，模板只读公开列 ----
    "web.py::setup": (1, "初始化页成员表，只选公开列"),
    "web.py::dev_switch_identity": (1, "开发模式切身份，只取 employee_code"),
    "web.py::_employee_code_of": (1, "只取 employee_code"),
    "web.py::_member_id_for_code": (1, "只取 member_id"),
    "web.py::_active_employee_codes": (1, "只取在职员工编号"),
    "web.py::_review_view": (2, "审核页联表取 reviewer 姓名"),
    "web.py::interface_board": (1, "看板联表取最近进展的填报人姓名"),
    "web.py::<module>": (1, "授权清单页的大 SQL，联表取 display_name"),
    "web.py::admin_grants": (1, "授权页候选人下拉，只选公开列"),
    "web.py::admin_create_grant": (1, "建授权前取 member_id / display_name / employment_status"),
    "web.py::admin_audit": (1, "审计页联表取 actor_name"),
    "web.py::settings_home": (1, "设置页最近动态，联表取 actor_name"),
}


def _personnel_access_points() -> dict[str, int]:
    """扫出代码里真实的访问点，口径写在上面那段注释里。"""

    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "oams"
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if QUALIFIED_TABLE not in source:
            continue
        tree = ast.parse(source)
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and QUALIFIED_TABLE in node.value):
                continue
            cursor: ast.AST = node
            owner = "<module>"
            while cursor in parent:
                cursor = parent[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = cursor.name
                    break
            key = f"{path.name}::{owner}"
            found[key] = found.get(key, 0) + 1
    return found


def test_every_personnel_table_access_point_is_registered():
    """碰 `workforce_members` 的地方必须一条条登记，并写清为什么可以。

    这条闸门挡的是「闸门只装在一个门上」——新开一扇窗就得在这里留下理由，
    否则下一个人以为整张表都脱敏了。
    """

    actual = _personnel_access_points()
    registered = PERSONNEL_ACCESS_POINTS

    unregistered = sorted(set(actual) - set(registered))
    assert not unregistered, (
        "这些地方访问了 workforce_members 但没登记（新开的窗？）：\n"
        + "\n".join(f"  {key}  x{actual[key]}" for key in unregistered)
        + "\n登记到 PERSONNEL_ACCESS_POINTS，并写清为什么不会把内部字段带进响应。")

    stale = sorted(set(registered) - set(actual))
    assert not stale, (
        "清单里登记了、代码里已经没有的访问点（删了没销账）：\n"
        + "\n".join(f"  {key}" for key in stale))

    mismatched = sorted(
        (key, registered[key][0], actual[key]) for key in actual if registered[key][0] != actual[key])
    assert not mismatched, (
        "访问点条数对不上（在这个函数里又加/删了一条查询）：\n"
        + "\n".join(f"  {key}  登记 {want} 条，实际 {got} 条" for key, want, got in mismatched))

    for key, (count, reason) in registered.items():
        assert count > 0 and reason.strip(), f"{key} 的登记不完整（条数或理由为空）"


def _personnel_sql_strings() -> list[tuple[str, str]]:
    """碰人员表的 SQL 字符串，连同「哪个文件的哪个函数」，用来查 `*`。"""

    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "oams"
    found: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        if QUALIFIED_TABLE not in source:
            continue
        tree = ast.parse(source)
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and QUALIFIED_TABLE in node.value):
                continue
            cursor: ast.AST = node
            owner = "<module>"
            while cursor in parent:
                cursor = parent[cursor]
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = cursor.name
                    break
            found.append((f"{path.name}::{owner}", node.value))
    return found


STAR_SELECT = re.compile(r"\bSELECT\s+\*", re.IGNORECASE)
ALIAS_STAR = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.\*")


def test_personnel_queries_never_select_star():
    """碰人员表的 SQL 里不许出现 `SELECT *` 或 `别名.*`。

    显式列今天是习惯，习惯会松：写的人图省事敲一个 `*`，脱敏分类里那句「internal
    永不返回」就靠运气了——整行取出来、`dict_row` 一序列化就出站。所以把它变成机器守的。

    **这道闸门不管什么**（写在前面，免得下一个人以为它全自动）：
      · 它只看有没有 `*`，看不出「显式列里挑了 token_version」——那件事靠上面
        `PERSONNEL_ACCESS_POINTS` 每条理由的人工复核；
      · 它不解析列清单。这里的 SQL 有联表、子查询、CTE，解析器写出来必然误报，
        误报三次就会有人加白名单，闸门就废了。窄而准，比宽而假强。
    """

    offenders: list[str] = []
    for key, sql in _personnel_sql_strings():
        for pattern, label in ((STAR_SELECT, "SELECT *"), (ALIAS_STAR, "别名.*")):
            match = pattern.search(sql)
            if match:
                excerpt = " ".join(sql.split())
                offenders.append(f"  {key}  含 {label}（{match.group(0)!r}）：{excerpt[:110]}")

    assert not offenders, (
        "这些地方用 `*` 取人员表，整行会漏出内部列：\n"
        + "\n".join(offenders)
        + "\n改成显式列；确实要整行，先想清楚它会不会进响应或审计。")


# ---------------------------------------------------------------------------
# 读明文要留痕：谁在什么时候看了谁的敏感字段
# ---------------------------------------------------------------------------
#
# 前面几节管的是「能不能看、看到的是不是打码」，这一节管「看了之后查不查得到」。
# 脱敏的四件里这是最后一件：没有它，读了明文也说不清是谁读的。
#
# 口径（2026-11-09 定）：只在**真解锁**时写一条；打码的不写（翻名单几百行会把真
# 记录淹掉）；一次调用一条，字段键排好序进 metadata；明文值一个字都不进审计。


def _add_member(url: str, employee_code: str, display_name: str) -> str:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        return str(conn.execute(
            """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
               VALUES (%s, %s, 'active') RETURNING member_id""",
            (employee_code, display_name)).fetchone()["member_id"])


def _sensitive_read_events(url: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            """SELECT actor_member_id, subject_type, subject_id, reason, metadata
                 FROM oams.workforce_audit_events
                WHERE event_type = 'sensitive_field.read'
                ORDER BY occurred_at""").fetchall()


def test_reading_a_sensitive_field_writes_exactly_one_event(fresh, monkeypatch):
    """有授权、列里真有敏感键 → 恰好一条，actor / subject / keys 都对得上。

    今天人员表里没有敏感列，所以这里把「这些列里有敏感键」这一层合成进来喂函数；
    判权与写审计走的都是真代码、真登记表。
    """

    import psycopg

    import oams.security as security_module

    suite._cli(["db", "init"], fresh)
    monkeypatch.setattr(security_module, "sensitive_keys_in", lambda columns: frozenset({"salary"}))
    viewer = _add_member(fresh, "VIEWER", "看的人")
    subject = _add_member(fresh, "SUBJECT", "被看的人")
    _grant(fresh, viewer, security_module.SENSITIVE_FIELD_RESOURCE, "read")

    from psycopg.rows import dict_row

    # 生产里的连接一律是 dict_row（`oams.db.connection`），闸门读登记表时按键取值。
    with psycopg.connect(fresh, autocommit=True, row_factory=dict_row) as conn:
        readable = security_module.readable_sensitive_fields(
            conn, viewer, subject, ("member_id", "salary"), via="api")
    assert readable == frozenset({"salary"}), readable

    events = _sensitive_read_events(fresh)
    assert len(events) == 1, events
    event = events[0]
    assert event["subject_type"] == "member"
    assert str(event["actor_member_id"]) == viewer
    assert str(event["subject_id"]) == subject
    assert event["metadata"] == {"field_keys": ["salary"], "via": "api"}, event["metadata"]


def test_masked_reads_write_no_event(fresh, monkeypatch):
    """没授权 → 打码 → 零条。打码也记的话，翻一遍名单就是几百行噪声。"""

    import psycopg

    import oams.security as security_module

    suite._cli(["db", "init"], fresh)
    monkeypatch.setattr(security_module, "sensitive_keys_in", lambda columns: frozenset({"salary"}))
    viewer = _add_member(fresh, "VIEWER", "看的人")
    subject = _add_member(fresh, "SUBJECT", "被看的人")

    from psycopg.rows import dict_row

    with psycopg.connect(fresh, autocommit=True, row_factory=dict_row) as conn:
        readable = security_module.readable_sensitive_fields(
            conn, viewer, subject, ("member_id", "salary"))
    assert readable == frozenset(), readable
    assert _sensitive_read_events(fresh) == []


def test_the_members_list_writes_no_sensitive_read_event(served_members):
    """真接口上翻一遍成员名单，审计表零新增——今天这六个字段一个都不敏感。

    这条盯的是噪声，也是回归：哪天有人给 `MEMBER_READ_FIELDS` 加了一列敏感字段、
    又忘了闸门，这里会先红。
    """

    token = _token_for(served_members["url"], "ADMIN001")
    _call(served_members["base"], "POST", "/v1/members", token,
          {"employee_code": "M001", "display_name": "甲"})
    _call(served_members["base"], "POST", "/v1/members", token,
          {"employee_code": "M002", "display_name": "乙"})

    status, rows = _call(served_members["base"], "GET", "/v1/members", token)
    assert status == 200, rows
    assert len(rows) >= 3, rows
    assert _sensitive_read_events(served_members["url"]) == []
