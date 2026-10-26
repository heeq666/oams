"""Three levels, two views.

The model is deliberately small: a company administrator, the administrator of one
or more departments, and everybody else. The level is read from the grants, never
from a job title, and the two progress screens are the places where the difference
has to be visible:

* progress centre -- everybody sees the company roll-up, the office floor and the
  detail of their own departments; administrators see every department;
* reports centre -- staff see what they themselves submitted, department
  administrators see their departments, the company administrator sees everything.

The board tests publish a report of their own instead of leaning on the seeded one:
a suite that runs in one session against one database must not depend on which
earlier test rewrote the sample rows.

Pure arithmetic runs anywhere; the rest needs a seeded database and skips without one.
"""
import json
import urllib.error
import urllib.request
import uuid

import pytest

from workhub.config import settings
from workhub.security import (DEV_WORKER_COOKIE, NOTICE_COOKIE, ROLE_DEPARTMENT, ROLE_STAFF,
                              ROLE_SYSTEM, admin_level, administered_org_unit_ids,
                              forget_dev_identities)


@pytest.fixture
def shortcut(monkeypatch):
    """Turn the design-stage shortcut on, and leave no roster cached behind us."""
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    forget_dev_identities()
    yield
    forget_dev_identities()


def _one(sql: str, params=()):
    from workhub.db import connection

    with connection() as conn:
        return conn.execute(sql, params).fetchone()


def _member_id(employee_code: str):
    """Resolve a seeded employee in the database the app is currently pointed at."""
    row = _one("""SELECT member_id FROM workhub.workforce_members
                   WHERE employee_code = %s AND employment_status = 'active'""", (employee_code,))
    if row is None:
        pytest.skip(f"no {employee_code} in the database the app is pointed at")
    return row["member_id"]


def _level_of(employee_code: str) -> str:
    from workhub.db import connection

    with connection() as conn:
        return admin_level(conn, _member_id(employee_code))


def _departments_of(employee_code: str) -> set[str]:
    from workhub.db import connection

    with connection() as conn:
        ids = administered_org_unit_ids(conn, _member_id(employee_code))
        if not ids:
            return set()
        rows = conn.execute("SELECT name FROM workhub.org_units WHERE org_unit_id = ANY(%s)",
                            (list(ids),)).fetchall()
    return {row["name"] for row in rows}


def _open(url: str, employee_code: str | None = None):
    """GET a page as a chosen member.

    The identity cookie is the whole mechanism the switcher uses, so sending it
    directly exercises the same path the badge does without going through the menu.
    """
    headers = {"Cookie": f"{DEV_WORKER_COOKIE}={employee_code}"} if employee_code else {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _publish(org_code: str, employee_code: str, label: str):
    """Publish one active interface with one live entry, owned by *employee_code*."""
    from workhub.db import connection

    member = _one("SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s",
                  (employee_code,))
    org = _one("SELECT org_unit_id FROM workhub.org_units WHERE org_code = %s", (org_code,))
    if member is None or org is None:
        pytest.skip(f"seed lacks {employee_code} or {org_code}")
    name = f"角色验证-{label}-{uuid.uuid4().hex[:6]}"
    with connection() as conn:
        interface = conn.execute(
            """INSERT INTO workhub.interfaces
                   (org_unit_id, slug, name, status, schema_version, descriptor,
                    created_by, updated_by)
               VALUES (%s, %s, %s, 'active', '0.2', %s, %s, %s)
               RETURNING interface_id""",
            (org["org_unit_id"], f"role-check-{uuid.uuid4().hex[:8]}", name,
             json.dumps({"schema_version": "0.2", "name": name}), member["member_id"],
             member["member_id"])).fetchone()
        conn.execute(
            """INSERT INTO workhub.interface_entries
                   (interface_id, title, status, reported_by, entry_mode, status_entry, reported_at)
               VALUES (%s, %s, 'in_progress', %s, 'live', 'active', now())""",
            (interface["interface_id"], f"{label}-001", member["member_id"]))
    return interface["interface_id"], name


def _unpublish(interface_id) -> None:
    from workhub.db import connection

    with connection() as conn:
        conn.execute("DELETE FROM workhub.interface_entries WHERE interface_id = %s", (interface_id,))
        conn.execute("DELETE FROM workhub.interfaces WHERE interface_id = %s", (interface_id,))


# ---------------------------------------------------------------------------
# The level itself
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_the_seeded_roster_sits_at_three_levels(shortcut, site):
    assert _level_of("ADMIN001") == ROLE_SYSTEM
    # HR_MANAGER may change other people's work items, which is department
    # administration -- and it stops there, because the settings pages stay shut.
    assert _level_of("MVP1001") == ROLE_DEPARTMENT
    assert _level_of("MVP1002") == ROLE_STAFF


@pytest.mark.db
def test_a_department_administrator_runs_only_their_departments(shortcut, site):
    # Managing something is what makes somebody an administrator; asking for their
    # departments must then name exactly the ones they are assigned to.  Staff get
    # an empty set -- "no departments" and "I run no departments" are the same answer.
    assert _departments_of("MVP1001") == {"人事部"}
    assert _departments_of("MVP1002") == set()


# ---------------------------------------------------------------------------
# Progress centre: company roll-up for everyone, department detail by level
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_progress_centre_shows_every_department_to_an_administrator(shortcut, site):
    for code in ("ADMIN001", "MVP1001"):
        status, body = _open(site + "/progress", code)
        assert status == 200, (code, status)
        # Seeded work items live in 人事部 and 业务部; both must be on a manager's page.
        assert "人事部" in body and "业务部" in body, code


@pytest.mark.db
def test_progress_centre_limits_staff_to_their_own_department(shortcut, site):
    status, body = _open(site + "/progress", "MVP1002")
    assert status == 200
    assert "人事部" in body
    # The roll-up numbers stay company-wide -- that is the point of the top band --
    # but the per-department detail below them must not leak another department.
    assert "业务部" not in body
    assert "只显示你所属的部门" in body


@pytest.mark.db
def test_progress_centre_tells_each_level_what_it_is_looking_at(shortcut, site):
    _, admin_body = _open(site + "/progress", "ADMIN001")
    assert "你是系统管理员" in admin_body
    _, manager_body = _open(site + "/progress", "MVP1001")
    assert "你是部门管理员" in manager_body


# ---------------------------------------------------------------------------
# Reports centre: my own / my departments / everything
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_reports_centre_shows_every_department_to_the_company_administrator(shortcut, site):
    interface_id, name = _publish("HR", "MVP1001", "管理员可见")
    try:
        status, body = _open(site + "/interfaces/board", "ADMIN001")
        assert status == 200
        assert name in body
        assert "能看到所有部门的汇报" in body
    finally:
        _unpublish(interface_id)


@pytest.mark.db
def test_reports_centre_shows_their_departments_to_a_department_administrator(shortcut, site):
    mine, mine_name = _publish("HR", "MVP1001", "本部门")
    elsewhere, elsewhere_name = _publish("SALES", "MVP2001", "别部门")
    try:
        status, body = _open(site + "/interfaces/board", "MVP1001")
        assert status == 200
        assert mine_name in body
        assert elsewhere_name not in body
        assert "只能看到你负责的部门的汇报" in body
    finally:
        _unpublish(mine)
        _unpublish(elsewhere)


@pytest.mark.db
def test_reports_centre_hides_other_departments_from_staff(shortcut, site):
    interface_id, name = _publish("HR", "MVP1001", "主管的汇报")
    try:
        status, body = _open(site + "/interfaces/board", "MVP2001")
        assert status == 200
        # Staff see submissions, not departments: somebody else's report is invisible
        # to them even though it is a perfectly ordinary live entry.
        assert name not in body
        assert "只能看到自己提交的汇报" in body
    finally:
        _unpublish(interface_id)


@pytest.mark.db
def test_a_staff_member_reads_the_entry_they_submitted(shortcut, site):
    mine, mine_name = _publish("HR", "MVP1002", "我自己填的")
    theirs, theirs_name = _publish("SALES", "MVP2001", "同事填的")
    try:
        status, body = _open(site + "/interfaces/board", "MVP1002")
        assert status == 200
        assert mine_name in body
        assert theirs_name not in body

        # The same row is a department detail for whoever runs that department.
        _, manager_body = _open(site + "/interfaces/board", "MVP1001")
        assert mine_name in manager_body and theirs_name not in manager_body
    finally:
        _unpublish(mine)
        _unpublish(theirs)


# ---------------------------------------------------------------------------
# Office floor: a company-wide picture, open to everybody
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_the_office_floor_is_open_to_staff(shortcut, site):
    status, body = _open(site + "/office", "MVP1002")
    assert status == 200
    assert "办公室" in body

# ---------------------------------------------------------------------------
# A door that does not open is a detour, not a dead end
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the 303 so a test can read where the server wanted to send us."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_head(url: str, employee_code: str | None = None, cookie: str | None = None):
    """GET without following the redirect, so the detour itself is observable."""
    if cookie:
        headers = {"Cookie": cookie}
    elif employee_code:
        headers = {"Cookie": f"{DEV_WORKER_COOKIE}={employee_code}"}
    else:
        headers = {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.headers, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", "replace")


def test_the_detour_lands_on_the_first_screen_the_session_can_read():
    import workhub.api  # noqa: F401  -- web imports api; web alone is a half-built module
    from types import SimpleNamespace

    from workhub.web import _landing_url

    def landing(**flags):
        caps = {"departments": False, "progress": False, "review": False, "admin": False}
        caps.update(flags)
        return _landing_url(SimpleNamespace(state=SimpleNamespace(nav=caps)))

    # Same order as the brand link: the most useful screen this person can open.
    assert landing(progress=True, review=True, admin=True) == "/progress"
    assert landing(review=True, admin=True) == "/reviews"
    assert landing(admin=True) == "/settings"
    assert landing(departments=True) == "/departments"
    # Nothing matched: the board is the one screen every signed-in member gets.
    assert landing() == "/board"


def test_a_refused_form_keeps_the_explanation_but_a_refused_page_does_not():
    import workhub.api  # noqa: F401
    from starlette.requests import Request

    from workhub.web import _denied

    def scope(method):
        return {"type": "http", "method": method, "path": "/x", "raw_path": b"/x",
                "headers": [], "query_string": b"", "scheme": "http",
                "server": ("testserver", 80), "client": ("127.0.0.1", 1)}

    # A GET is a bookmark or a typed URL: send them where they can read something.
    seen = _denied(Request(scope("GET")), "需要权限。")
    assert seen.status_code == 303
    # A POST is a click they just made: redirecting silently would look like a no-op.
    kept = _denied(Request(scope("POST")), "需要权限。")
    assert kept.status_code == 403


@pytest.mark.db
def test_a_page_you_may_not_open_sends_you_to_one_you_can(shortcut, site):
    status, headers, body = _open_head(site + "/settings", "MVP1002")
    assert status == 303
    assert headers["location"].endswith("/progress")
    # The reason travels as a one-shot cookie, not as a wall of 403.
    assert NOTICE_COOKIE in (headers.get("set-cookie") or "")
    assert "没有权限" not in body


@pytest.mark.db
def test_the_administrator_still_gets_the_page_they_asked_for(shortcut, site):
    status, _, body = _open_head(site + "/settings", "ADMIN001")
    assert status == 200
    assert "设置" in body


@pytest.mark.db
def test_the_detour_explains_itself_once(shortcut, site):
    # The redirect carries the reason in a cookie; the landing page prints it and
    # clears it in the same response. Following the hop by hand (rather than with
    # an opener) is what makes both halves observable.
    _, headers, _ = _open_head(site + "/settings", "MVP1002")
    notice = (headers.get("set-cookie") or "").split(";")[0]
    assert notice.startswith(f"{NOTICE_COOKIE}=")

    status, landed_headers, body = _open_head(
        site + "/progress", cookie=f"{DEV_WORKER_COOKIE}=MVP1002; {notice}")
    assert status == 200
    assert "设置页只有管理员可以打开" in body
    # One shot: the page that shows the line also expires the cookie, so a reload
    # does not repeat a message about a click that already went nowhere.
    assert "workhub_notice=" in (landed_headers.get("set-cookie") or "")

