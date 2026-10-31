"""Setup-page pilot flow on an isolated database: department -> position -> member -> report."""
import os, re, sys, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


import pytest  # noqa: E402  (test-module wrapper)


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
    import psycopg
    from oams.security import issue_session
    base = os.environ.get('OAMS_PILOT_BASE', 'http://127.0.0.1:8142')
    # The administrator is looked up rather than configured: the bootstrap member
    # is whoever the seeded database happens to have created.
    admin_id = os.environ.get('OAMS_PILOT_ADMIN_ID')
    suffix = str(int(time.time()))[-6:]
    org_code = 'PILOTHR' + suffix
    position_code = 'PILOT_LEAD' + suffix
    employee_code = 'PILOT' + suffix
    with psycopg.connect(os.environ['OAMS_DATABASE_URL']) as conn:
        company = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE unit_type='company'").fetchone()[0]
        if not admin_id:
            admin_id = str(conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code='ADMIN001'").fetchone()[0])
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    def request(opener, path, method='GET', form=None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(base + path, data=payload, method=method)
        if payload is not None: req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with opener.open(req, timeout=15) as resp: return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as err: return err.code, err.read().decode(), dict(err.headers)
    def client(member_id):
        token, _ = issue_session(member_id)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect, urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _, _ = request(opener, '/login', 'POST', {'token': token})
        assert status == 302, status
        return opener
    def tokens(html):
        return re.findall(r'name="csrf" value="([^"]+)"', html)
    admin = client(admin_id)
    status, page, _ = request(admin, '/setup')
    assert status == 200 and '试用设置' in page, (status, page[:400])
    print('PASS setup page opens for administrator')
    status, _, headers = request(admin, '/setup/org-units', 'POST',
        {'csrf': tokens(page)[0], 'org_code': org_code, 'name': '试点部门' + suffix,
         'parent_org_unit_id': str(company), 'unit_type': 'department'})
    assert status == 303 and 'saved=org' in headers.get('location', ''), (status, headers)
    with psycopg.connect(os.environ['OAMS_DATABASE_URL']) as conn:
        dept = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code=%s", (org_code,)).fetchone()[0]
    print('PASS department created from setup page')
    status, page, _ = request(admin, '/setup')
    status, _, headers = request(admin, '/setup/positions', 'POST',
        {'csrf': tokens(page)[1], 'position_code': position_code, 'title': '试点负责人', 'org_unit_id': str(dept)})
    assert status == 303 and 'saved=position' in headers.get('location', ''), (status, headers)
    with psycopg.connect(os.environ['OAMS_DATABASE_URL']) as conn:
        position = conn.execute("SELECT position_id FROM oams.positions WHERE position_code=%s", (position_code,)).fetchone()[0]
    print('PASS position created from setup page')
    status, page, _ = request(admin, '/setup')
    status, body, headers = request(admin, '/setup/pilot', 'POST',
        {'csrf': tokens(page)[2], 'display_name': '试点负责人' + suffix,
         'employee_code': employee_code, 'position_id': str(position)})
    assert status == 303, (status, body[:300])
    loc = headers.get('location', '')
    assert 'saved=pilot' in loc and 'login=' in loc, loc
    login_token = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)['login'][0]
    with psycopg.connect(os.environ['OAMS_DATABASE_URL']) as conn:
        member = conn.execute("SELECT member_id, employment_status FROM oams.workforce_members WHERE employee_code=%s", (employee_code,)).fetchone()
        assignment = conn.execute("SELECT 1 FROM oams.position_assignments WHERE member_id=%s AND position_id=%s AND status='active'", (member[0], position)).fetchone()
        grants = conn.execute("SELECT count(*) FROM oams.permission_grants WHERE position_id=%s AND revoked_at IS NULL", (position,)).fetchone()[0]
    assert member[1] == 'active' and assignment is not None and grants >= 5, (member, assignment, grants)
    print('PASS pilot member is active, seated and granted reporting')
    pilot = client(str(member[0]))
    status, reports, _ = request(pilot, '/reports')
    assert status == 200, (status, reports[:300])
    status, _, _ = request(pilot, '/progress')
    assert status == 200
    print('PASS pilot member can open reporting and progress pages')
    status, _, _ = request(admin, '/setup/pilot', 'POST',
        {'csrf': 'bad-token', 'display_name': '乙', 'employee_code': employee_code + 'X', 'position_id': str(position)})
    assert status == 403, status
    print('PASS setup form rejects forged token')


def test_pilot_setup(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
