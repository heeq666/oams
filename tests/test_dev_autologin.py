"""The design-stage shortcut: no login screen.

```WORKHUB_DEV_AUTOLOGIN=ADMIN001``` signs every session-less request in as that
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
import urllib.request

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from workhub.config import settings
from workhub.security import (current_principal, dev_autologin_target, dev_principal,
                              principal_for_token, principal_from_request)


def _request(path: str = "/board", cookie: str | None = None) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{settings.web_session_cookie}={cookie}".encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8000),
    })


@pytest.fixture()
def shortcut(monkeypatch):
    """Point the shortcut at a chosen employee_code (or switch it off)."""

    def _set(value):
        monkeypatch.setattr(settings, "dev_autologin", value, raising=False)

    _set("ADMIN001")
    return _set


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
# The real resolution path (database)
# ---------------------------------------------------------------------------

def _admin_member_id():
    """The id the app itself would resolve for ADMIN001, in its own database.

    Deliberately not the conftest fixture: an in-process test server repoints
    settings.database_url at a throw-away database, so "ADMIN001" means a
    different member depending on which test runs first. Asking the connection the
    app is actually using keeps the assertion about the code path, not the seed.
    """
    from workhub.db import connection

    with connection() as conn:
        row = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s AND employment_status = 'active'",
            ("ADMIN001",)).fetchone()
    if row is None:
        pytest.skip("no ADMIN001 member in the database the app is pointed at")
    return row["member_id"]


@pytest.mark.db
def test_cookieless_request_is_signed_in(shortcut):
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
# `workhub_server`) on purpose: the autouse settings_follow_the_test fixture only
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
