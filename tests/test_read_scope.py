"""读权的边界：部门级读者只看得到自己范围内的东西。

2026-11-09 实测出来的洞：给一个成员只授 GA 部门范围的 `workforce.members/read`，
他打 `GET /v1/members` 读到全公司的人（含人事部的）。根因不是漏了 where，是那几条
读端点连范围参数都没有——`require_permission` 只回答「有没有这个权」，不回答
「这个权到哪个部门为止」。

口径（C经理定，别自己改）：
1. 有一条全公司范围（`scope_type='all'`、effect=allow）的对应授权 → 看全公司，照旧；
2. 只有部门级授权或岗位座位 → 只返回范围里的，范围外的不出现（不是打码）；
3. 既无座位也无全公司授权 → 200 空列表，不报错；
4. 范围判断只在 `security.readable_org_unit_ids` 一处做，端点只调它。

这个文件里最要紧的是那几条**对照**：同一个人、同一棵树，只换一条授权，结果就得
不一样。只断言「部门级读者少看到人」是验不出东西的——把收敛整段删掉，断言照样过。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# 世界：公司 + 两个部门（GA / HR），每部门一个岗位，一个部门级读者
# ---------------------------------------------------------------------------

WORLD_SQL = """
-- 两个平级部门
INSERT INTO oams.org_units (org_code, name, unit_type)
VALUES ('GA', '行政部', 'department'), ('HR', '人事部', 'department');

-- 各一个岗位
INSERT INTO oams.positions (position_code, title, org_unit_id)
SELECT 'GA_STAFF', '行政专员', org_unit_id FROM oams.org_units WHERE org_code = 'GA';
INSERT INTO oams.positions (position_code, title, org_unit_id)
SELECT 'HR_STAFF', '人事专员', org_unit_id FROM oams.org_units WHERE org_code = 'HR';

-- 两个人：一个在 GA，一个在 HR（谁在哪个部门由岗位说了算）
-- employment_status 默认是 pending，签票要求 active，所以这里显式写死
INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
VALUES ('GA001', '张行政', 'active'), ('HR001', '李人事', 'active');

INSERT INTO oams.position_assignments
       (member_id, position_id, assignment_type, status, starts_at, reason, approved_at, approved_by)
SELECT m.member_id, p.position_id, 'primary', 'active', now() - interval '1 day', '测试用', now(), m.member_id
  FROM oams.workforce_members m, oams.positions p
 WHERE m.employee_code = 'GA001' AND p.position_code = 'GA_STAFF';

INSERT INTO oams.position_assignments
       (member_id, position_id, assignment_type, status, starts_at, reason, approved_at, approved_by)
SELECT m.member_id, p.position_id, 'primary', 'active', now() - interval '1 day', '测试用', now(), m.member_id
  FROM oams.workforce_members m, oams.positions p
 WHERE m.employee_code = 'HR001' AND p.position_code = 'HR_STAFF';
"""


@pytest.fixture(scope="module")
def world(db_ready):
    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            ["bootstrap-admin", "--name", "管理员", "--employee-code", "ADMIN001",
             "--org-code", "COMPANY", "--org-name", "公司"],
            url,
        )
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(WORLD_SQL)
            # 读者：只有 GA 的岗位，先不给任何授权
            conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES ('READER', '只看行政', 'active')""")
            conn.execute(
                """INSERT INTO oams.position_assignments
                       (member_id, position_id, assignment_type, status, starts_at, reason,
                        approved_at, approved_by)
                   SELECT m.member_id, p.position_id, 'concurrent', 'active',
                          now() - interval '1 day', '测试用', now(), m.member_id
                     FROM oams.workforce_members m, oams.positions p
                    WHERE m.employee_code = 'READER' AND p.position_code = 'GA_STAFF'""")
            # 另一个读者：没有任何岗位（用来验「范围内没有人」那条）
            conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES ('FLOATER', '没岗位的人', 'active')""")
        with suite.serve(url) as base:
            yield {"url": url, "base": base}
    finally:
        suite._drop_database(db_ready, name)


def _member_id(url: str, employee_code: str) -> str:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return str(conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
            (employee_code,)).fetchone()["member_id"])


def _grant(url: str, employee_code: str, resource: str, action: str,
           scope_type: str = "org_unit", org_code: str = "GA") -> None:
    """给这个人在某个范围发一条 allow 授权。"""
    member_id = _member_id(url, employee_code)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO oams.permission_grants
                   (subject_type, member_id, resource, action, scope_type, scope_org_unit_id,
                    effect, valid_from, valid_until, reason, approved_by, approved_at)
               VALUES ('member', %s, %s, %s, %s,
                       CASE WHEN %s = 'self_position' THEN NULL
                            ELSE (SELECT org_unit_id FROM oams.org_units WHERE org_code = %s) END,
                       'allow', now() - interval '1 day', now() + interval '30 days',
                       '测试用', %s, now())""",
            (member_id, resource, action, scope_type, scope_type, org_code, member_id),
        )


def _token(url: str, employee_code: str) -> str:
    from oams.security import issue_session

    token, _ = issue_session(_member_id(url, employee_code))
    return token


def _get(base: str, path: str, token: str):
    request = urllib.request.Request(base + path)
    request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def _codes(rows, key: str = "employee_code") -> set[str]:
    return {row[key] for row in rows}


# ---------------------------------------------------------------------------
# ① 部门级读者：跨部门的人不出现（这是那个洞本身）
# ---------------------------------------------------------------------------


def test_a_department_reader_does_not_see_people_from_another_department(world):
    _grant(world["url"], "READER", "workforce.members", "read")
    status, rows = _get(world["base"], "/v1/members", _token(world["url"], "READER"))
    assert status == 200, rows
    codes = _codes(rows)
    # 本部门的同事与他自己在，人事部的人不在
    assert "HR001" not in codes, codes
    assert "李人事" not in json.dumps(rows, ensure_ascii=False)
    assert "GA001" in codes, codes


def test_the_company_admin_still_sees_everybody(world):
    """收紧不能把管理员自己也滤掉——这条是防「拦得太狠」。"""

    admin = _token(world["url"], "ADMIN001")
    status, rows = _get(world["base"], "/v1/members", admin)
    assert status == 200, rows
    codes = _codes(rows)
    assert {"ADMIN001", "GA001", "HR001", "READER"} <= codes, codes


def test_a_reader_without_any_department_gets_an_empty_list(world):
    """有读权，但范围内没有人：200 空列表，不是 403 也不是 500。"""

    # 给的是 self_position 档：他不指向任何部门、又没有岗位，读范围就是空的
    _grant(world["url"], "FLOATER", "workforce.members", "read", scope_type="self_position")
    status, rows = _get(world["base"], "/v1/members", _token(world["url"], "FLOATER"))
    assert status == 200, rows
    assert rows == [], rows


def test_a_member_with_no_seat_only_appears_for_company_level_readers(world):
    """没有任何岗位的人不属于任何部门——部门级读者看不到他，公司级看得到。

    这条是**故意**的：按部门收窄时，没挂岗位的人本来就落在所有人范围外，
    若为了「别漏人」把他放进来，那等于整张名单又多了一条绕过范围的路径。
    """

    reader_rows = _get(world["base"], "/v1/members", _token(world["url"], "READER"))[1]
    assert "FLOATER" not in _codes(reader_rows), _codes(reader_rows)

    admin_rows = _get(world["base"], "/v1/members", _token(world["url"], "ADMIN001"))[1]
    assert "FLOATER" in _codes(admin_rows), _codes(admin_rows)


# ---------------------------------------------------------------------------
# ② 同一件事换一条授权，结果就得变（只断言「少看到人」验不出收敛）
# ---------------------------------------------------------------------------


def test_the_same_reader_sees_the_whole_company_once_given_company_scope(world):
    """把同一个人换成公司级授权：同一端点立刻看到人事部的人。

    这条是那个洞的**正向对照**——它证明「少看到人」确实来自范围收敛，
    而不是因为库里本来就没有那些人。
    """

    reader = _token(world["url"], "READER")
    before = _codes(_get(world["base"], "/v1/members", reader)[1])
    assert "HR001" not in before, before

    with psycopg.connect(world["url"], autocommit=True) as conn:
        conn.execute(
            """INSERT INTO oams.permission_grants
                   (subject_type, member_id, resource, action, scope_type, effect,
                    valid_from, valid_until, reason, approved_by, approved_at)
               VALUES ('member', %s, 'workforce.members', 'read', 'all', 'allow',
                       now() - interval '1 day', now() + interval '30 days', '测试用', %s, now())""",
            (_member_id(world["url"], "READER"), _member_id(world["url"], "READER")),
        )
    try:
        after = _codes(_get(world["base"], "/v1/members", reader)[1])
        assert "HR001" in after, after
    finally:
        # 收回这条授权：它是本文件里唯一一条 members 的全公司授权，
        # 留着会把后面「指定范围外部门应该被拒」那条静默变成全公司读者。
        with psycopg.connect(world["url"], autocommit=True) as conn:
            conn.execute(
                """DELETE FROM oams.permission_grants
                    WHERE member_id = %s AND resource = 'workforce.members' AND scope_type = 'all'""",
                (_member_id(world["url"], "READER"),),
            )


# ---------------------------------------------------------------------------
# ③ 其余三个读端点同口径
# ---------------------------------------------------------------------------


def test_the_department_list_is_narrowed_too(world):
    _grant(world["url"], "READER", "workforce.org_units", "read")
    status, rows = _get(world["base"], "/v1/org-units", _token(world["url"], "READER"))
    assert status == 200, rows
    codes = {row["org_code"] for row in rows}
    assert codes == {"GA"}, codes

    admin_codes = {row["org_code"] for row in
                   _get(world["base"], "/v1/org-units", _token(world["url"], "ADMIN001"))[1]}
    assert {"GA", "HR", "COMPANY"} <= admin_codes, admin_codes


def test_positions_are_narrowed_too(world):
    _grant(world["url"], "READER", "workforce.positions", "read")
    reader = _get(world["base"], "/v1/positions", _token(world["url"], "READER"))[1]
    assert {row["position_code"] for row in reader} == {"GA_STAFF"}, reader

    admin = _get(world["base"], "/v1/positions", _token(world["url"], "ADMIN001"))[1]
    assert {"GA_STAFF", "HR_STAFF"} <= {row["position_code"] for row in admin}


def test_assignments_are_narrowed_too(world):
    _grant(world["url"], "READER", "workforce.assignments", "read")
    reader = _get(world["base"], "/v1/assignments", _token(world["url"], "READER"))[1]
    # 他自己也在 GA 挂了一条 concurrent 任命，所以两条都在
    assert {row["display_name"] for row in reader} == {"张行政", "只看行政"}, reader

    admin = _get(world["base"], "/v1/assignments", _token(world["url"], "ADMIN001"))[1]
    assert {"张行政", "李人事"} <= {row["display_name"] for row in admin}


def test_grants_are_narrowed_too(world):
    """部门级读者只看到落到自己部门的授权行；公司级那些不出现。"""

    _grant(world["url"], "READER", "workforce.grants", "read")
    _grant(world["url"], "GA001", "workforce.reports", "read", org_code="GA")
    _grant(world["url"], "HR001", "workforce.reports", "read", org_code="HR")

    status, rows = _get(world["base"], "/v1/grants", _token(world["url"], "READER"))
    assert status == 200, rows
    subjects = {str(row["member_id"]) for row in rows}
    assert _member_id(world["url"], "HR001") not in subjects, subjects
    assert _member_id(world["url"], "GA001") in subjects, subjects

    admin_rows = _get(world["base"], "/v1/grants", _token(world["url"], "ADMIN001"))[1]
    admin_subjects = {str(row["member_id"]) for row in admin_rows}
    assert {_member_id(world["url"], "GA001"), _member_id(world["url"], "HR001")} <= admin_subjects


# ---------------------------------------------------------------------------
# ④ 按件读一个部门：范围外要 403 说人话，不是静默返回空
# ---------------------------------------------------------------------------


def test_reading_one_department_outside_the_range_is_refused_in_chinese(world):
    """列表端点按范围裁剪，单个部门的读端点则要开口拒绝。

    两者是不同的失败方式：列表少几行是正常的（范围外不出现），而指名要一个范围外的
    部门，得让他知道「不是没有数据，是这儿不归你看」，否则他会以为这个部门是空的。
    """

    _grant(world["url"], "READER", "workforce.work_items", "read")
    with psycopg.connect(world["url"], row_factory=dict_row) as conn:
        hr = str(conn.execute(
            "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'HR'").fetchone()["org_unit_id"])
    status, body = _get(world["base"], f"/v1/board/org-units/{hr}", _token(world["url"], "READER"))
    assert status == 403, body
    assert "这个部门不在你的范围内" in body["detail"], body
