"""敏感字段登记表：`oams db init` 之后这张表就得是能用的。

脱敏的整条链路（哪些字段算敏感 → 谁能读 → 读时打码、逐条记审计）都从这张表出发，
所以它不能是空的。这张表在 `schema.sql` 里建了，可**建表不等于有数据**——2026-11-09
查过一次：表在、行数 0、代码里 `grep sensitive_field` 也是 0 处，等于一条从没通电的链路。

这里的用例只钉「种子」这一层，而且**走文档上的那条命令**（`oams db init`）在一个独立新库上
验，不是在本机那个「碰巧被人灌过」的开发库上验——那正是上一轮读权收敛踩过的坑：
用例造什么数据就只验什么数据。
"""

from __future__ import annotations

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
