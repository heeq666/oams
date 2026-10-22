"""End-to-end: author a department interface, fill it in, read it from the manager view.

Runs against an isolated database and a local server. This is the check that the
last mile works: a validated descriptor becomes a page a person can fill, and what
they type reaches the manager's view without the department's private fields.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


import pytest  # noqa: E402  (test-module wrapper)

from workhub.upgrade import upgrade_descriptor  # noqa: E402


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from workhub.security import issue_session

    base = os.environ.get("WORKHUB_IFACE_BASE", "http://127.0.0.1:8145")
    failures = []

    def check(name, condition, detail=""):
        if condition:
            print("PASS", name)
        else:
            failures.append(name)
            print("FAIL", name, detail)

    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-sampling",
        "name": "设计部打样进度",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "modules": [
            {"key": "item", "type": "item_card", "title": "事项", "order": 1,
             "source_fields": ["title"], "aggregatable": True},
            {"key": "state", "type": "metric_card", "title": "状态", "order": 2,
             "source_fields": ["state_word"], "aggregatable": True},
            {"key": "blocked", "type": "blocked_card", "title": "卡点", "order": 3,
             "source_fields": ["blocked_reason"], "aggregatable": True},
            {"key": "evidence", "type": "evidence_input", "title": "依据", "order": 4,
             "source_fields": ["evidence_ref"], "aggregatable": True},
            {"key": "private", "type": "list_block", "title": "面料备注", "order": 5,
             "source_fields": ["fabric_note"], "aggregatable": False},
        ],
        "fields": [
            {"key": "title", "label": "款号", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "AW24-001"},
            {"key": "state_word", "label": "打样状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "打样中",
             "options": ["打样中", "等面料", "已完成"]},
            {"key": "blocked_reason", "label": "卡在哪", "type": "text", "required": False,
             "map_to": "blocked_reason", "aggregatable": True, "example": "等面料到仓"},
            {"key": "evidence_ref", "label": "打样单号", "type": "text", "required": True,
             "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
            {"key": "fabric_note", "label": "面料备注", "type": "text", "required": False,
             "map_to": "display_only", "aggregatable": False, "example": "待补货"},
        ],
        "status_map": {"打样中": "in_progress", "等面料": "blocked", "已完成": "done"},
        "rules": {"evidence_required": True},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }
    # 0.1 descriptors are legal at creation time but no longer render: the renderer
    # only knows the 0.2 module whitelist.  Upgrade first, so these scripts keep
    # testing the flow instead of the migration.
    descriptor, _ = upgrade_descriptor(descriptor)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"]) as conn:
        admin = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'ADMIN001'").fetchone()[0]
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'DESIGN'").fetchone()[0]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def request(opener, path, method="GET", form=None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(base + path, data=payload, method=method)
        if payload is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(), dict(err.headers)

    def client(member_id):
        token, _ = issue_session(str(member_id))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                             urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _, _ = request(opener, "/login", "POST", {"token": token})
        assert status == 302, status
        return opener

    def tokens(html):
        return re.findall(r'name="csrf" value="([^"]+)"', html)

    admin_client = client(admin)
    status, page, _ = request(admin_client, "/setup/interfaces")
    check("interface setup page opens", status == 200 and "界面制作" in page, status)

    status, _, headers = request(admin_client, "/setup/interfaces", "POST", {
        "csrf": tokens(page)[0], "org_unit_id": str(org), "slug": "design-sampling",
        "name": "设计部打样进度", "position_id": "", "descriptor_text": json.dumps(descriptor),
    })
    location = headers.get("location", "")
    check("valid descriptor accepted", status == 303 and ("saved=1" in location or "error=" not in location),
          location[:200])

    bad = dict(descriptor)
    bad["status_map"] = {"打样中": "in_progress", "等面料": "waiting"}
    status, page, _ = request(admin_client, "/setup/interfaces")
    status, body, headers = request(admin_client, "/setup/interfaces", "POST", {
        "csrf": tokens(page)[0], "org_unit_id": str(org), "slug": "design-bad",
        "name": "错误界面", "position_id": "", "descriptor_text": json.dumps(bad),
    })
    location = headers.get("location", "")
    check("unmapped status rejected", status == 303 and "error=" in location and "error=json" not in location,
          location[:160])

    status, page, _ = request(admin_client, "/interfaces")
    check("interface listed for the department", status == 200 and "设计部打样进度" in page, status)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"]) as conn:
        interface_id = conn.execute(
            """SELECT interface_id FROM workhub.interfaces
                WHERE slug='design-sampling' AND status='active'""").fetchone()[0]

    status, page, _ = request(admin_client, f"/interfaces/{interface_id}")
    check("fill page renders descriptor", status == 200 and "款号" in page and "打样单号" in page, status)

    status, _, headers = request(admin_client, f"/interfaces/{interface_id}", "POST", {
        "csrf": tokens(page)[0], "title": "AW24-001", "state_word": "等面料",
        "blocked_reason": "", "evidence_ref": "SY-2410", "fabric_note": "待补货",
    })
    check("blocked without reason rejected", "error=" in headers.get("location", ""),
          headers.get("location", "")[:160])

    status, page, _ = request(admin_client, f"/interfaces/{interface_id}")
    status, _, headers = request(admin_client, f"/interfaces/{interface_id}", "POST", {
        "csrf": tokens(page)[0], "title": "AW24-001", "state_word": "等面料",
        "blocked_reason": "等面料到仓，供应商承诺 10-20", "evidence_ref": "SY-2410",
        "fabric_note": "待补货",
    })
    check("valid entry accepted", status == 303 and "saved=1" in headers.get("location", ""),
          headers.get("location", "")[:200])

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"]) as conn:
        entry = conn.execute(
            """SELECT title, status, blocked_reason, evidence, extra, reported_by
                 FROM workhub.interface_entries WHERE interface_id = %s""", (interface_id,)).fetchone()
    check("standard fields stored", entry[0] == "AW24-001" and entry[1] == "blocked" and entry[3] == "SY-2410", entry)
    check("department-private field kept aside", entry[4].get("fabric_note") == "待补货", entry[4])
    check("reporter recorded", str(entry[5]) == str(admin))

    status, board, _ = request(admin_client, "/interfaces/board")
    check("manager board shows the report", status == 200 and "AW24-001" in board and "等面料到仓" in board, status)
    check("manager board hides private field", "待补货" not in board)

    status, page, _ = request(admin_client, f"/interfaces/{interface_id}")
    status, body, _ = request(admin_client, f"/interfaces/{interface_id}", "POST", {
        "csrf": "not-the-token", "title": "X", "state_word": "打样中", "evidence_ref": "Y"})
    check("forged form token rejected", status == 403, status)

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all department interface checks passed")


def test_department_interface(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
