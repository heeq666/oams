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
            # 另一个人：在 FIN 有正式任职（有座位），本身没有任何授权行。
            # 用来钉「有座位的人拿到 all 也不会多进一个部门」。
            seated = conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES (%s, '坐了岗位的人', 'active') RETURNING member_id""",
                ("SEATED01",)).fetchone()
            conn.execute(
                """INSERT INTO oams.position_assignments
                       (member_id, position_id, assignment_type, status, starts_at, reason)
                   SELECT %s, p.position_id, 'primary', 'active', now() - interval '1 day',
                          '本来就在这个部门上班'
                     FROM oams.positions p WHERE p.position_code = 'FIN_LEAD'""",
                (seated["member_id"],))
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "units": units,
                   "member_id": member_id, "admin_member_id": str(admin_id),
                   "seated_member_id": str(seated["member_id"])}
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


# ---------------------------------------------------------------------------
# 三、三条到期与收回的规矩（此前一条用例都没有）
# ---------------------------------------------------------------------------


def test_a_deny_cancels_the_allow_on_the_same_department(world) -> None:
    """同一部门上 allow 与 deny 同时有效：deny 赢。

    这是「临时收权」的做法——不删原来的 allow，补一条 deny 盖过它，审计里两条都在。
    以前全仓测试没有一条 deny（`git grep deny tests/` 空），这条路径改坏了没人会发现。
    """
    from oams.security import accessible_org_unit_ids, has_permission

    member = world["member_id"]
    ga = world["units"]["GA"]
    assert has_permission_ga(world) is True
    _sql(world, """INSERT INTO oams.permission_grants
                       (subject_type, member_id, resource, action, scope_type, scope_org_unit_id,
                        effect, reason, valid_until)
                   VALUES ('member', %s, 'workforce.reports', 'submit', 'org_unit', %s,
                           'deny', '临时停掉他的提交权', now() + interval '1 day')""",
         (member, ga))
    try:
        with _conn(world) as conn:
            assert has_permission(conn, member, "workforce.reports", "submit",
                                  org_unit_id=ga) is False
            # FIN 上没有被 deny，原来那条照样有效
            assert has_permission(conn, member, "workforce.reports", "submit",
                                  org_unit_id=world["units"]["FIN"]) is True
            # 进部门的范围也跟着收窄：GA 不再出现在他能进的列表里
            assert ga not in {str(item) for item in accessible_org_unit_ids(conn, member)}
    finally:
        _sql(world, """DELETE FROM oams.permission_grants
                        WHERE member_id = %s AND effect = 'deny'""", (member,))


def test_a_revoked_grant_stops_working_at_once(world) -> None:
    """收回之后立刻失效——不是等下次登录，也不是等缓存过期。

    注意要连同 `read` 一起收回：能进哪些部门是**不分 resource 的**（`accessible_org_unit_ids`
    把这个人所有授权行一起算），只收回 `submit` 他照样进得了 FIN，只是不能填。
    """
    from oams.security import has_permission

    member = world["member_id"]
    fin = world["units"]["FIN"]
    assert has_permission_fin(world) is True
    _sql(world, """UPDATE oams.permission_grants SET revoked_at = now()
                    WHERE member_id = %s AND resource = 'workforce.reports'
                      AND scope_org_unit_id = %s""", (member, fin))
    try:
        with _conn(world) as conn:
            assert has_permission(conn, member, "workforce.reports", "submit",
                                  org_unit_id=fin) is False
        # 真接口同样立刻拒：判据不是只在函数里对
        status, payload = _build(world, _token(member), "FIN", "scope-fin-after-revoke")
        assert status == 403, payload
    finally:
        _sql(world, """UPDATE oams.permission_grants SET revoked_at = NULL
                        WHERE member_id = %s AND resource = 'workforce.reports'
                          AND scope_org_unit_id = %s""", (member, fin))


def test_an_expired_grant_stops_working_without_anybody_touching_it(world) -> None:
    """到期就失效，不需要谁去点收回。

    做法不是往后改日期（那要等真的过期），是把 `valid_until` 挪到过去——判据是
    `now() < valid_until`，与「时间已经把这条抛在后面」是同一件事。同样要 `read`
    和 `submit` 一起改，理由同上一条。
    """
    from oams.security import has_permission

    member = world["member_id"]
    fin = world["units"]["FIN"]
    _sql(world, """UPDATE oams.permission_grants
                      SET valid_from = now() - interval '2 days',
                          valid_until = now() - interval '1 day'
                    WHERE member_id = %s AND resource = 'workforce.reports'
                      AND scope_org_unit_id = %s""", (member, fin))
    try:
        with _conn(world) as conn:
            assert has_permission(conn, member, "workforce.reports", "submit",
                                  org_unit_id=fin) is False
        status, payload = _build(world, _token(member), "FIN", "scope-fin-after-expiry")
        assert status == 403, payload
    finally:
        _sql(world, """UPDATE oams.permission_grants
                          SET valid_from = now(),
                              valid_until = now() + interval '30 days'
                        WHERE member_id = %s AND resource = 'workforce.reports'
                          AND scope_org_unit_id = %s""", (member, fin))


def test_a_company_grant_does_not_widen_somebody_who_has_a_position(world) -> None:
    """有岗位的人拿到 `all`，能进的部门一个都不多出来。

    这不是 bug，是 `accessible_org_unit_ids` 的第一条边界（`app/oams/security.py:396`）：
    `all` 只在**没有岗位座位**的人身上才摊成全部在用部门，否则设计部负责人拿到一条全公司
    授权就会看见行政部的页。钉住它是因为「成员与权限」那屏第一版给了这个勾选框
    （`5072381`），勾了却不生效——界面骗人比没有这个功能更坏，2026-11-05 已撤掉。
    """
    from oams.security import accessible_org_unit_ids, all_department_ids, can_access_org_unit

    seated = world["seated_member_id"]
    it = world["units"]["IT"]
    with _conn(world) as conn:
        assert can_access_org_unit(conn, seated, it) is False
        before = {str(item) for item in accessible_org_unit_ids(conn, seated)}
    _sql(world, """INSERT INTO oams.permission_grants
                       (subject_type, member_id, resource, action, scope_type, reason, valid_until)
                   VALUES ('member', %s, 'workforce.reports', 'submit', 'all',
                           '给这个人开全公司（探针）', now() + interval '1 day')""",
         (seated,))
    try:
        with _conn(world) as conn:
            after = {str(item) for item in accessible_org_unit_ids(conn, seated)}
            assert after == before, "有岗位的人拿到 all，能进的部门不该变"
            assert can_access_org_unit(conn, seated, it) is False
            assert len(all_department_ids(conn)) > len(before)
    finally:
        _sql(world, """DELETE FROM oams.permission_grants
                        WHERE member_id = %s AND scope_type = 'all'""", (seated,))


def has_permission_ga(world) -> bool:
    from oams.security import has_permission

    with _conn(world) as conn:
        return has_permission(conn, world["member_id"], "workforce.reports", "submit",
                              org_unit_id=world["units"]["GA"])


def has_permission_fin(world) -> bool:
    from oams.security import has_permission

    with _conn(world) as conn:
        return has_permission(conn, world["member_id"], "workforce.reports", "submit",
                              org_unit_id=world["units"]["FIN"])


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
