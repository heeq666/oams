"""权限解析：全公司（`all`）与指定部门（`org_unit`）两档，以及访问判据的唯一出口。

`scope_type='org_unit'` 的授权曾经等于不存在（`effective_grants` 写死只认 `all`），
「超级管理员只管子部门」这一档因此做不出来。这里钉三件事：

1. 指定部门的授权真生效，而且只在它点名的那些部门里生效；
2. 「这个人能进哪些部门」= 岗位所在的 ∪ 被授权管的，全公司范围等于每一个在用部门；
3. 建页与看页走同一个判据——不是函数级自测，是拿票打真接口。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[1]
DESCRIPTOR = json.loads(
    (REPO / "data" / "neutral-demo-ga-parcel-lookup.json").read_text(encoding="utf-8")
)

NARROW = "SCOPE01"  # 只被授权管 FIN 与 GA 的人
ADMIN = "ADMIN001"


# ---------------------------------------------------------------------------
# 一套自己的世界：一个公司、三个部门、一个只在两个部门里有授权的人
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(db_ready):
    import psycopg
    from psycopg.rows import dict_row

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            ["bootstrap-admin", "--name", "管理员", "--employee-code", ADMIN,
             "--org-code", "COMPANY", "--org-name", "公司"],
            url,
        )
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            company = conn.execute(
                "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'COMPANY'").fetchone()
            units = {}
            for code, title in (("FIN", "财务部"), ("GA", "行政部"), ("IT", "信息部")):
                row = conn.execute(
                    """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                       VALUES (%s, %s, 'department', %s) RETURNING org_unit_id""",
                    (code, title, company["org_unit_id"])).fetchone()
                units[code] = str(row["org_unit_id"])
                conn.execute(
                    """INSERT INTO oams.positions (position_code, title, org_unit_id)
                       SELECT %s, %s, %s""",
                    (f"{code}_LEAD", title + "负责人", row["org_unit_id"]))
            member = conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES (%s, '范围测试员', 'active') RETURNING member_id""",
                (NARROW,)).fetchone()
            member_id = str(member["member_id"])
            # 只在 FIN 与 GA 上有权建页：这两条就是「窄授权」。
            for code in ("FIN", "GA"):
                conn.execute(
                    """INSERT INTO oams.permission_grants
                           (subject_type, member_id, resource, action, scope_type,
                            scope_org_unit_id, reason, valid_until)
                       VALUES ('member', %s, 'workforce.reports', %s, 'org_unit', %s,
                               '只在这两个部门里干活', now() + interval '30 days')""",
                    (member_id, "read", units[code]))
                conn.execute(
                    """INSERT INTO oams.permission_grants
                           (subject_type, member_id, resource, action, scope_type,
                            scope_org_unit_id, reason, valid_until)
                       VALUES ('member', %s, 'workforce.reports', %s, 'org_unit', %s,
                               '只在这两个部门里干活', now() + interval '30 days')""",
                    (member_id, "submit", units[code]))
            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
                (ADMIN,)).fetchone()["member_id"]
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "units": units,
                   "member_id": member_id, "admin_member_id": str(admin_id)}
    finally:
        suite._drop_database(db_ready, name)


def _sql(world, sql: str, args=()):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(world["url"], row_factory=dict_row) as conn:
        cursor = conn.execute(sql, args)
        rows = cursor.fetchall() if cursor.description is not None else []
        conn.commit()
    return rows


def _call(base: str, method: str, path: str, token: str,
          payload: dict | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw


def _token(member_id: str) -> str:
    from oams.security import issue_session

    token, _ = issue_session(member_id)
    return token


def _descriptor_for(org_code: str) -> dict:
    """样本是 GA 的；换部门就是把顶层码与指标命名空间一起换掉。"""

    text = json.dumps(DESCRIPTOR, ensure_ascii=False).replace("dept.GA.", f"dept.{org_code}.")
    text = text.replace('"GA_LEAD"', f'"{org_code}_LEAD"')
    descriptor = json.loads(text)
    descriptor["org_unit_code"] = org_code
    return descriptor


def _build(world, token: str, org_code: str, slug: str) -> tuple[int, object]:
    return _call(world["base"], "POST", "/v1/interfaces", token, {
        "slug": slug, "name": "范围验收 " + org_code,
        "org_unit_code": org_code, "descriptor": _descriptor_for(org_code),
    })


# ---------------------------------------------------------------------------
# 一、解析层：窄授权不是「等于没有」
# ---------------------------------------------------------------------------


def test_a_grant_that_names_two_departments_is_read_back(world) -> None:
    from oams.security import effective_grants

    with _conn(world) as conn:
        grants = effective_grants(conn, world["member_id"], "workforce.reports", "read")
    named = {str(grant["scope_org_unit_id"]) for grant in grants}
    assert named == {world["units"]["FIN"], world["units"]["GA"]}


def test_permission_can_be_asked_about_one_department(world) -> None:
    from oams.security import has_permission

    with _conn(world) as conn:
        assert has_permission(conn, world["member_id"], "workforce.reports", "read",
                              org_unit_id=world["units"]["FIN"]) is True
        assert has_permission(conn, world["member_id"], "workforce.reports", "read",
                              org_unit_id=world["units"]["GA"]) is True
        assert has_permission(conn, world["member_id"], "workforce.reports", "read",
                              org_unit_id=world["units"]["IT"]) is False


def test_company_scope_still_means_the_whole_company(world) -> None:
    from oams.security import has_company_scope

    with _conn(world) as conn:
        assert has_company_scope(conn, world["member_id"]) is False
        assert has_company_scope(conn, world["admin_member_id"]) is True


def test_accessible_departments_are_positions_plus_grants(world) -> None:
    from oams.security import accessible_org_unit_ids

    with _conn(world) as conn:
        assert {str(item) for item in accessible_org_unit_ids(conn, world["member_id"])} == {
            world["units"]["FIN"], world["units"]["GA"]}
        # 公司级管理员不是靠「能进哪些部门」进来的（他没有座位也没有窄授权），
        # 而是 has_company_scope 直接放行——判据要问 can_access_org_unit 才看得见。
        from oams.security import can_access_org_unit

        assert can_access_org_unit(conn, world["admin_member_id"], world["units"]["IT"]) is True


def test_a_company_wide_grant_covers_every_department(world) -> None:
    from oams.security import accessible_org_unit_ids

    with _conn(world) as conn:
        conn.execute(
            """INSERT INTO oams.permission_grants
                   (subject_type, member_id, resource, action, scope_type, reason, valid_until)
               VALUES ('member', %s, 'workforce.reports', 'read', 'all', '临时给全公司', now() + interval '1 day')""",
            (world["member_id"],))
        conn.commit()
        reached = {str(item) for item in accessible_org_unit_ids(conn, world["member_id"])}
        assert world["units"]["IT"] in reached
        conn.execute(
            """DELETE FROM oams.permission_grants
                WHERE member_id = %s AND scope_type = 'all' AND resource = 'workforce.reports'""",
            (world["member_id"],))
        conn.commit()


# ---------------------------------------------------------------------------
# 二、判据层：拿票打真接口（空库自造数据，不依赖任何种子）
# ---------------------------------------------------------------------------


def test_a_page_can_be_built_in_a_granted_department(world) -> None:
    status, payload = _build(world, _token(world["member_id"]), "FIN", "scope-fin-page")
    assert status == 201, payload
    assert payload["org_unit_id"] == world["units"]["FIN"]


def test_the_same_person_cannot_build_in_a_department_they_were_not_given(world) -> None:
    status, payload = _build(world, _token(world["member_id"]), "IT", "scope-it-page")
    assert status == 403
    assert "你不在这个部门的范围内" in json.dumps(payload, ensure_ascii=False)


def test_a_page_can_be_read_only_inside_the_granted_departments(world) -> None:
    token = _token(world["admin_member_id"])
    status, page = _build(world, token, "IT", "scope-it-read")
    assert status == 201, page
    inside, _ = _call(world["base"], "GET", f"/v1/interfaces/{page['interface_id']}",
                      _token(world["member_id"]))
    assert inside == 403
    status, fin = _build(world, token, "FIN", "scope-fin-read")
    assert status == 201, fin
    inside, body = _call(world["base"], "GET", f"/v1/interfaces/{fin['interface_id']}",
                         _token(world["member_id"]))
    assert inside == 200, body


def test_a_company_wide_grant_opens_the_other_departments_too(world) -> None:
    _sql(world, """INSERT INTO oams.permission_grants
                       (subject_type, member_id, resource, action, scope_type, reason, valid_until)
                   VALUES ('member', %s, 'workforce.reports', 'read', 'all',
                           '临时给全公司', now() + interval '1 day')""",
         (world["member_id"],))
    try:
        status, page = _build(world, _token(world["member_id"]), "IT", "scope-it-all")
        assert status == 201, page
        assert page["org_unit_id"] == world["units"]["IT"]
    finally:
        _sql(world, """DELETE FROM oams.permission_grants
                        WHERE member_id = %s AND scope_type = 'all'
                          AND resource = 'workforce.reports'""", (world["member_id"],))


def test_a_system_administrator_is_not_narrowed_by_any_of_this(world) -> None:
    status, page = _build(world, _token(world["admin_member_id"]), "FIN", "scope-admin-fin")
    assert status == 201, page


def _conn(world):
    import psycopg

    class _Ctx:
        def __enter__(self):
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(world["url"], row_factory=dict_row)
            return self.conn

        def __exit__(self, *exc):
            self.conn.rollback()
            self.conn.close()
            return False

    return _Ctx()
