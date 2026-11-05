"""The design-stage shortcut: no login screen.

```OAMS_DEV_AUTOLOGIN=ADMIN001``` signs every session-less request in as that
member. The point of these tests is not that it works, but that it stays a
*who* and never becomes a *what*:

* off is the default, and off means the login screen still refuses a bare request;
* a wrong bearer token is still refused even when the shortcut is on, so an expired
  session stays visible instead of being silently upgraded;
* the shortcut principal carries no session row, so nothing may try to revoke one.

Pure parsing tests run without a database; the rest need one and skip otherwise.
"""
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from oams.config import settings
from oams.security import (DEV_ROLE_ADMIN, DEV_ROLE_MANAGER, DEV_ROLE_STAFF, DEV_WORKER_COOKIE,
                              ROLE_DEPARTMENT, ROLE_RANK, ROLE_STAFF, ROLE_SYSTEM,
                              assert_dev_autologin_is_local, current_principal,
                              dev_autologin_refusal, dev_autologin_target, dev_identities,
                              dev_principal, dev_role_label, principal_for_token,
                              principal_from_request)


def _request(path: str = "/board", cookie: str | None = None,
             dev_cookie: str | None = None,
             client: tuple[str, int] | None = ("127.0.0.1", 51234)) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{settings.web_session_cookie}={cookie}".encode()))
    if dev_cookie is not None:
        headers.append((b"cookie", f"{DEV_WORKER_COOKIE}={dev_cookie}".encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
        "client": client,
    })


@pytest.fixture()
def shortcut(monkeypatch):
    """Point the shortcut at a chosen employee_code (or switch it off)."""

    def _set(value):
        monkeypatch.setattr(settings, "dev_autologin", value, raising=False)

    _set("ADMIN001")
    return _set


# ---------------------------------------------------------------------------
# The shortcut has no login, so it is only allowed where only this machine looks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bind_host, refused", [
    # Loopback: the only place with no login is safe.
    ("127.0.0.1", False),
    ("127.0.0.5", False),
    ("::1", False),
    ("localhost", False),
    ("  127.0.0.1  ", False),
    # Reachable from somewhere else -- every one of these is a refusal.
    ("0.0.0.0", True),
    ("::", True),
    ("[::]", True),
    ("198.51.100.10", True),
    ("198.51.100.11", True),
    ("oams.example[.]local", True),
])
def test_the_shortcut_is_only_allowed_on_a_loopback_bind(monkeypatch, bind_host, refused):
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    monkeypatch.setattr(settings, "bind_host", bind_host, raising=False)
    refusal = dev_autologin_refusal()
    assert (refusal is not None) is refused, (bind_host, refusal)
    if refused:
        # And it really does stop signing people in: the check is not decoration.
        assert dev_autologin_target() is None
        assert dev_autologin_target(_request()) is None
        assert dev_identities() == []
        with pytest.raises(SystemExit):
            assert_dev_autologin_is_local()
    else:
        assert dev_autologin_target() == "ADMIN001"
        assert_dev_autologin_is_local()  # 不抛


def test_a_caller_from_another_machine_gets_no_shortcut(monkeypatch):
    """`uvicorn --host` 能绕过设置，所以按请求来源再判一次。

    The settings may still say 127.0.0.1 while the command line bound elsewhere;
    only the request itself knows where the browser really came from.
    """
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1", raising=False)
    assert dev_autologin_target(_request(client=("198.51.100.10", 5011))) is None
    # 本机来的照常，否则本机试用就没意义了。
    assert dev_autologin_target(_request(client=("127.0.0.1", 5011))) == "ADMIN001"
    assert dev_autologin_target(_request(client=("::1", 5011))) == "ADMIN001"


def test_an_unreadable_client_address_does_not_turn_the_shortcut_off(monkeypatch):
    """只有确凿证据才拒绝：进程内测试客户端的来源叫 `testserver`，那不是 IP。

    把「读不懂就当外网」写成判据，会让所有进程内用例莫名失去免登录，而且报告里
    看不出是这条规则干的——比漏判更难查。
    """
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1", raising=False)
    assert dev_autologin_target(_request(client=("testserver", 80))) == "ADMIN001"
    assert dev_autologin_target(_request(client=None)) == "ADMIN001"


def test_switching_the_shortcut_off_needs_no_bind_change(monkeypatch):
    """关掉之后，绑哪儿都不该拦：拦的是「没登录还对外开放」这件事，不是端口。"""
    monkeypatch.setattr(settings, "dev_autologin", "0", raising=False)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0", raising=False)
    assert dev_autologin_refusal() is None
    assert dev_autologin_target() is None
    assert_dev_autologin_is_local()


def test_the_badge_does_not_claim_a_shortcut_that_was_refused(monkeypatch):
    """角标必须和这个人实际拿到的身份一致。

    请求被拒时页面显示的是登录表单；这时候还挂「开发模式 · 免登录」就是在最该说
    实话的地方说假话——它会让人以为自己是管理员，下一步操作却全是 401。
    """
    from oams import api as _api  # noqa: F401  先载入 api，否则单 import web 会撞环形导入
    from oams.web import TEMPLATES

    dev_mode = TEMPLATES.env.globals["dev_mode"]
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1", raising=False)
    assert dev_mode(_request(client=("127.0.0.1", 5011))) is True
    assert dev_mode(_request(client=("198.51.100.10", 5011))) is False
    # 不传 request 时仍按设置说话：模板里的每一处调用都带 request，这里是兜底语义。
    assert dev_mode() is True


# ---------------------------------------------------------------------------
# Parsing: the flag is a single well-defined string, not a fuzzy boolean
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("0", None),
    ("false", None),
    ("off", None),
    ("none", None),
    ("1", "ADMIN001"),
    ("true", "ADMIN001"),
    ("TRUE", "ADMIN001"),
    ("yes", "ADMIN001"),
    ("on", "ADMIN001"),
    ("ADMIN001", "ADMIN001"),
    ("ADMIN001 ", "ADMIN001"),
    ("design001", "design001"),
])
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "dev_autologin", raw, raising=False)
    assert dev_autologin_target() == expected


def test_off_means_nobody_is_signed_in(monkeypatch):
    monkeypatch.setattr(settings, "dev_autologin", None, raising=False)
    assert dev_principal() is None
    assert principal_from_request(_request()) is None


# ---------------------------------------------------------------------------
# The identity switch: a cookie that only ever names a *who*
# ---------------------------------------------------------------------------

def test_the_chosen_identity_beats_the_environment_default(monkeypatch):
    monkeypatch.setattr(settings, "dev_autologin", "ADMIN001", raising=False)
    request = _request(dev_cookie="STAFF001")
    assert dev_autologin_target(request) == "STAFF001"


def test_the_choice_is_ignored_while_the_shortcut_is_off(monkeypatch):
    # A cookie left in the browser from yesterday must not sign anybody in once the
    # switch is off -- otherwise "restore login" would depend on clearing cookies.
    monkeypatch.setattr(settings, "dev_autologin", "0", raising=False)
    request = _request(dev_cookie="STAFF001")
    assert dev_autologin_target(request) is None
    assert dev_principal(request) is None


def test_no_roster_is_exposed_while_the_shortcut_is_off(monkeypatch):
    monkeypatch.setattr(settings, "dev_autologin", None, raising=False)
    assert dev_identities() == []


@pytest.mark.parametrize("grants, expected", [
    ({"workforce.grants:manage", "workforce.members:manage"}, DEV_ROLE_ADMIN),
    # Managing people or work items inside a department is department administration;
    # it never reaches company administration, because the settings pages stay shut.
    ({"workforce.members:manage"}, DEV_ROLE_MANAGER),
    ({"workforce.work_items:manage"}, DEV_ROLE_MANAGER),
    ({"workforce.interfaces:manage", "workforce.work_items:manage"}, DEV_ROLE_MANAGER),
    # Reviewing a page is reading, so it does not lift anybody out of the staff level.
    ({"workforce.interfaces:review", "workforce.reports:submit"}, DEV_ROLE_STAFF),
    ({"workforce.work_items:read"}, DEV_ROLE_STAFF),
    (set(), DEV_ROLE_STAFF),
    (None, DEV_ROLE_STAFF),
])
def test_role_label_reads_the_grants(grants, expected):
    # The label has to describe what the page will actually let them do, so it is
    # derived from grants -- not from the position title somebody typed.
    assert dev_role_label(grants) == expected


def test_the_three_levels_rank_from_company_to_staff():
    assert [ROLE_SYSTEM, ROLE_DEPARTMENT, ROLE_STAFF] == sorted(
        ROLE_RANK, key=lambda role: ROLE_RANK[role])


def test_switch_is_not_reachable_without_the_shortcut(monkeypatch):
    # api imports the web router at the bottom of its module; importing web first
    # would meet a half-built module, so load the app module the way serve does.
    import oams.api  # noqa: F401
    from oams.web import dev_switch_identity

    monkeypatch.setattr(settings, "dev_autologin", None, raising=False)
    with pytest.raises(HTTPException) as excinfo:
        dev_switch_identity(_request(), code="ADMIN001", next="/board")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# The real resolution path (database)
# ---------------------------------------------------------------------------

def _admin_member_id():
    """The id the app itself would resolve for ADMIN001, in its own database.

    Deliberately not the conftest fixture: an in-process test server repoints
    settings.database_url at a throw-away database, so "ADMIN001" means a
    different member depending on which test runs first. Asking the connection the
    app is actually using keeps the assertion about the code path, not the seed.
    """
    from oams.db import connection

    with connection() as conn:
        row = conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s AND employment_status = 'active'",
            ("ADMIN001",)).fetchone()
    if row is None:
        pytest.skip("no ADMIN001 member in the database the app is pointed at")
    return row["member_id"]


@pytest.mark.db
def test_cookieless_request_is_signed_in(shortcut, db_ready):
    expected = _admin_member_id()
    principal = principal_from_request(_request())
    assert principal is not None
    assert principal["member_id"] == expected
    assert principal["dev_autologin"] is True
    # No session row: nothing may be revoked, and no cookie is involved.
    assert principal["session_id"] is None


@pytest.mark.db
def test_unknown_employee_code_signs_nobody_in(shortcut):
    shortcut("NO-SUCH-EMPLOYEE-404")
    assert dev_principal() is None
    assert principal_from_request(_request()) is None


@pytest.mark.db
def test_bearer_channel_gets_the_same_shortcut(shortcut):
    expected = _admin_member_id()
    principal = current_principal(None)
    assert principal is not None and principal["member_id"] == expected


@pytest.mark.db
def test_wrong_bearer_token_is_still_refused(shortcut):
    # The shortcut covers "no credentials", not "bad credentials": a stale token
    # has to fail loudly, otherwise a revoked session looks like a working one.
    assert principal_for_token("definitely-not-a-session") is None
    with pytest.raises(HTTPException) as excinfo:
        current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials="definitely-not-a-session"))
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# Over HTTP: the screens the user actually opens
# ---------------------------------------------------------------------------

def _get(url: str):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                         urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.url
    except urllib.error.HTTPError as exc:  # 4xx keep their body for assertions
        return exc.code, exc.read().decode("utf-8", "replace"), exc.url


# These four talk to a served instance.  They name the `site` fixture (not
# `oams_server`) on purpose: the autouse settings_follow_the_test fixture only
# repoints the process settings for a test that names site / hr_site /
# migrated_site, and the app resolves the shortcut through those settings.  Asking
# for `site` is what keeps the assertion about this member, not whichever
# database a previous test left behind.

@pytest.mark.db
def test_board_opens_without_a_login(shortcut, site):
    expected = _admin_member_id()
    status, body, url = _get(site + "/board")
    assert status == 200, (status, url)
    assert "开发模式" in body and "免登录" in body
    # The badge names the member the shortcut resolved, so nobody can be looking
    # at another person's numbers thinking it is their own view.
    principal = principal_from_request(_request())
    assert principal is not None and principal["member_id"] == expected
    assert principal["display_name"] in body


@pytest.mark.db
def test_login_screen_is_skipped(shortcut, site):
    status, body, url = _get(site + "/login")
    assert status == 200
    assert url.endswith("/board"), url


@pytest.mark.db
def test_logout_says_it_cannot_stick(shortcut, site):
    request = urllib.request.Request(site + "/logout", method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        body = response.read().decode("utf-8", "replace")
        assert response.status == 200
    # It must not pretend to have signed out, and it must not bounce either.
    assert "自动登录回来" in body


@pytest.mark.db
def test_revoke_without_a_session_is_a_no_op(shortcut, site):
    request = urllib.request.Request(site + "/v1/sessions/current/revoke", method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        assert response.status == 204


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand the 303 back instead of following it, so Set-Cookie is inspectable."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_form(url: str, data: dict[str, str]):
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.headers, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", "replace")


@pytest.mark.db
def test_switching_identity_changes_who_the_app_recognises(shortcut, site):
    people = dev_identities()
    if len(people) < 2:
        pytest.skip("needs at least two active members to switch between")
    chosen = people[-1]

    status, headers, _ = _post_form(site + "/dev/identity",
                                    {"code": chosen["employee_code"], "next": "/board"})
    assert status == 303
    assert f"{DEV_WORKER_COOKIE}={chosen['employee_code']}" in (headers.get("Set-Cookie") or "")

    # The cookie is the whole mechanism: the next request renders as that person.
    principal = principal_from_request(_request(dev_cookie=chosen["employee_code"]))
    assert principal is not None
    assert principal["member_id"] == chosen["member_id"]
    assert principal["employee_code"] == chosen["employee_code"]


@pytest.mark.db
def test_the_roster_labels_each_person_by_what_they_can_do(shortcut, site):
    people = dev_identities()
    if not people:
        pytest.skip("no active members in this database")
    for person in people:
        assert person["role_label"] == dev_role_label(person["grants"])
        assert person["role_label"] in {DEV_ROLE_ADMIN, DEV_ROLE_MANAGER, DEV_ROLE_STAFF}
    # Strongest first: the switcher's first row should be the one with everything.
    order = {DEV_ROLE_ADMIN: 0, DEV_ROLE_MANAGER: 1, DEV_ROLE_STAFF: 2}
    ranks = [order[person["role_label"]] for person in people]
    assert ranks == sorted(ranks)


@pytest.mark.db
def test_an_unknown_code_never_becomes_a_cookie(shortcut, site):
    status, headers, _ = _post_form(site + "/dev/identity",
                                    {"code": "NO-SUCH-EMPLOYEE-404", "next": "/board"})
    assert status == 303
    assert DEV_WORKER_COOKIE not in (headers.get("Set-Cookie") or "")


@pytest.mark.db
def test_choosing_nothing_restores_the_startup_default(shortcut, site):
    status, headers, _ = _post_form(site + "/dev/identity", {"code": "", "next": "/board"})
    assert status == 303
    cookie = headers.get("Set-Cookie") or ""
    assert DEV_WORKER_COOKIE in cookie and "max-age=0" in cookie.lower()
