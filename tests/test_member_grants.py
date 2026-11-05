"""成员与权限屏：点名授权发得出去、越界整笔退回、收回只置时间不删行。

这一屏做的事是**运营动作**：给某个人开某几个部门。三件会真出事的情况各一条用例：

* 越界——部门管理员给别人的部门发授权，必须整笔拒绝，库里一行都不落（不是发一半）；
* 重复——同一个人同一件事同一个部门，第二行有效授权会让人说不清哪行算数，拒并指出已有那行；
* 收回——只置 `revoked_at`，行留着，审计里能一路对回去。

还有一条读的路：清单必须标清「被点名」和「岗位」两类来源，否则一屏授权看完不知道
哪条是这一页发的、哪条是岗位自带的。

数据一律自己造，整笔事务结束时回滚。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

import oams.api
from oams import web
from oams.config import settings

pytestmark = pytest.mark.db

PLATFORM = "ADMIN001"      # 系统管理员：workforce.grants/manage，全公司
DESIGN_LEAD = "DESIGN001"  # 部门管理员：只给 DESIGN 一个部门的管理范围
PLAIN = "MVP1002"          # 普通成员：自己没有任何授权（拿来被授权用）

GRANT_PURPOSE = "oams:grants-form:v1"
REVOKE_PURPOSE = "oams:grant-revoke-form:v1"


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


def _csrf(cookie: str, purpose: str) -> str:
    """表单令牌 = 会话 cookie 按用途签一次，与 `web._form_token` 同一把钥。"""
    return hmac.new(cookie.encode(), purpose.encode(), hashlib.sha256).hexdigest()


@pytest.fixture()
def room(seeded_database, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    def unit(code):
        row = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code = %s",
                           (code,)).fetchone()
        assert row is not None, f"{code} 不在种子库里"
        return row["org_unit_id"]

    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (PLATFORM, DESIGN_LEAD, PLAIN)}

    # 给设计负责人一条「只管 DESIGN 的 grants/manage」——种子里只有全公司级的
    # SYSADMIN，没有部门级的管理员，这条是用例自己的夹具，测完随事务回滚。
    conn.execute(
        """INSERT INTO oams.permission_grants
              (subject_type, member_id, resource, action, scope_type, scope_org_unit_id,
               valid_from, valid_until, reason)
           VALUES ('member', %s, 'workforce.grants', 'manage', 'org_unit', %s,
                   now() - interval '1 day', now() + interval '60 days', 'test fixture')""",
        (people[DESIGN_LEAD], unit("DESIGN")))

    state = {"me": PLATFORM, "cookie": "member-grants-test"}

    def _principal(request):
        return {"member_id": people[state["me"]],
                "employee_code": state["me"],
                "display_name": state["me"],
                "employment_status": "active"}

    monkeypatch.setattr(oams.api, "principal_from_request", _principal)

    def client() -> TestClient:
        made = TestClient(oams.api.app)
        made.cookies.set(settings.web_session_cookie, state["cookie"])
        return made

    def as_(code):
        state["me"] = code

    def page(**params):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return client().get("/admin/grants" + query, follow_redirects=False)

    def grant(*, member=None, capability="workforce.reports|submit", units=(), company=False,
              reason="需要看财务部的页面", until=None, csrf=None, who=None):
        form: dict[str, object] = {
            "csrf": _csrf(state["cookie"], GRANT_PURPOSE) if csrf is None else csrf,
            "member_id": str(member or people[PLAIN]),
            "capability": capability,
            "reason": reason,
            "valid_until": until or (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d"),
        }
        if units:
            form["org_unit_id"] = [str(unit) for unit in units]
        if company:
            form["company_wide"] = "1"
        return client().post("/admin/grants", data=form, follow_redirects=False)

    def revoke(grant_id, *, reason="离职交接", csrf=None):
        return client().post(f"/admin/grants/{grant_id}/revoke",
                             data={"csrf": _csrf(state["cookie"], REVOKE_PURPOSE) if csrf is None else csrf,
                                   "reason": reason}, follow_redirects=False)

    def rows(member=None):
        sql = """SELECT grant_id, subject_type, member_id, resource, action, scope_type,
                        scope_org_unit_id, valid_until, revoked_at, reason
                   FROM oams.permission_grants
                  WHERE resource IN ('workforce.reports', 'workforce.interfaces')
                    AND subject_type = 'member'"""
        params: list[object] = []
        if member is not None:
            sql += " AND member_id = %s"
            params.append(member)
        return conn.execute(sql + " ORDER BY created_at", params).fetchall()

    def audits(event_type="grant.created"):
        return conn.execute(
            """SELECT actor_member_id, subject_id, reason FROM oams.workforce_audit_events
                WHERE event_type = %s ORDER BY occurred_at, event_id""", (event_type,)).fetchall()

    def wipe():
        conn.execute("""DELETE FROM oams.permission_grants
                         WHERE subject_type = 'member'
                           AND resource IN ('workforce.reports', 'workforce.interfaces')""")
        conn.execute("""DELETE FROM oams.workforce_audit_events
                         WHERE event_type IN ('grant.created', 'grant.revoked')""")

    made = {"conn": conn, "client": client, "as_": as_, "page": page, "grant": grant,
            "revoke": revoke, "rows": rows, "audits": audits, "wipe": wipe,
            "people": people, "unit": unit,
            "design": unit("DESIGN"), "hr": unit("HR"), "sales": unit("SALES"),
            "company": unit("COMPANY")}
    try:
        yield made
    finally:
        conn.rollback()
        conn.close()


def _location(response) -> str:
    return urllib.parse.unquote(response.headers.get("location", ""))


def test_a_system_admin_grants_one_department_and_the_audit_remembers_it(room):
    """给自己范围内的部门发一条：落一行、审计里有 grant.created、原因原样存着。"""
    room["as_"](PLATFORM)
    room["wipe"]()

    posted = room["grant"](units=[room["hr"]], reason="接手供应商结算，需要看人事部的页面")
    assert posted.status_code == 303, posted.text
    assert _location(posted).startswith("/admin/grants?saved=1"), _location(posted)

    rows = room["rows"](room["people"][PLAIN])
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["resource"] == "workforce.reports" and row["action"] == "submit"
    assert row["scope_type"] == "org_unit" and row["scope_org_unit_id"] == room["hr"]
    assert row["revoked_at"] is None
    assert row["reason"] == "接手供应商结算，需要看人事部的页面"
    # 到期日是那天结束（本地时区 23:59:59），不是当天零点
    assert row["valid_until"].astimezone(web.DISPLAY_TZ).strftime("%H:%M:%S") == "23:59:59"

    events = room["audits"]("grant.created")
    assert len(events) == 1, events
    assert events[0]["subject_id"] == room["people"][PLAIN]
    assert events[0]["reason"] == "接手供应商结算，需要看人事部的页面"
    assert events[0]["actor_member_id"] == room["people"][PLATFORM]


def test_a_department_admin_cannot_reach_outside_their_scope(room):
    """部门管理员点别人的部门：403，而且库里一行都不落（不是发半截）。"""
    room["as_"](DESIGN_LEAD)
    room["wipe"]()

    refused = room["grant"](units=[room["hr"]])
    assert refused.status_code == 403, refused.text
    assert "人事部" in refused.text and "范围" in refused.text
    assert room["rows"]() == []
    assert room["audits"]("grant.created") == []

    # 自己的部门照发，同一份表单，一勾就成
    ok = room["grant"](units=[room["design"]])
    assert ok.status_code == 303 and _location(ok).startswith("/admin/grants?saved=1")
    assert len(room["rows"](room["people"][PLAIN])) == 1


def test_the_same_grant_twice_is_refused_and_points_at_the_existing_row(room):
    """同一个人同一件事同一个部门，第二行有效授权会被拒，并说清已有那行到哪天。"""
    room["as_"](PLATFORM)
    room["wipe"]()

    first = room["grant"](units=[room["sales"]])
    assert first.status_code == 303 and _location(first).startswith("/admin/grants?saved=1")

    again = room["grant"](units=[room["sales"]])
    assert again.status_code == 303, again.text
    location = _location(again)
    assert "error=" in location and "code=duplicate" in location, location
    assert "已经有这一条了" in location
    assert len(room["rows"](room["people"][PLAIN])) == 1

    # 一次勾两个部门：整体两条，不是「先落一条再说」
    both = room["grant"](units=[room["hr"], room["sales"]], capability="workforce.interfaces|review")
    assert both.status_code == 303 and _location(both).startswith("/admin/grants?saved=2")
    assert len(room["rows"](room["people"][PLAIN])) == 3


def test_revoking_keeps_the_row_and_only_stamps_the_time(room):
    """收回只置 revoked_at：行还在，审计里多一条 grant.revoked。"""
    room["as_"](PLATFORM)
    room["wipe"]()

    made = room["grant"](units=[room["design"]])
    assert made.status_code == 303
    grant_id = room["rows"](room["people"][PLAIN])[0]["grant_id"]

    revoked = room["revoke"](grant_id, reason="项目结束")
    assert revoked.status_code == 303, revoked.text
    assert _location(revoked).startswith("/admin/grants?saved=0"), _location(revoked)

    rows = room["rows"](room["people"][PLAIN])
    assert len(rows) == 1, rows
    assert rows[0]["grant_id"] == grant_id and rows[0]["revoked_at"] is not None
    events = room["audits"]("grant.revoked")
    assert len(events) == 1 and events[0]["reason"] == "项目结束"

    # 收第二次：不改任何东西，也不当成功
    twice = room["revoke"](grant_id)
    assert twice.status_code == 303
    assert "已经收回过了" in _location(twice)


def test_the_list_separates_named_grants_from_position_grants(room):
    """清单要能分清「被点名」和「岗位」：同一个人两种来源都在时，标签不能混。"""
    room["as_"](PLATFORM)
    room["wipe"]()
    assert room["grant"](units=[room["hr"]]).status_code == 303

    shown = room["page"]()
    assert shown.status_code == 200, shown.text
    assert "成员与权限" in shown.text
    assert "被点名" in shown.text
    # 种子里设计负责人的那些是岗位档，收不动，页面上要说清去哪里改
    assert "岗位" in shown.text


def test_a_manager_without_the_grant_is_turned_away(room):
    """没有任何 grants/manage 的人打不开这一页，也发不出去。"""
    room["as_"](PLAIN)
    room["wipe"]()

    # GET 不走 403：把人送回他能看的第一页（`_denied` 的既定行为），
    # 硬塞一张 403 页面上唯一的出口还是「回看板」，等于关门。
    opened = room["page"]()
    assert opened.status_code == 303, opened.status_code
    assert opened.headers["location"] != "/admin/grants"

    posted = room["grant"](units=[room["hr"]])
    assert posted.status_code == 403, posted.text
    assert room["rows"]() == []
