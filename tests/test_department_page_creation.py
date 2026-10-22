"""Department members can create their own page and see it immediately as a draft."""
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


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from psycopg.rows import dict_row
    from workhub.security import issue_session

    base = os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000")
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        member = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'DESIGN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'DESIGN'").fetchone()["org_unit_id"]
        other_org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'COMPANY'").fetchone()["org_unit_id"]

    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-weekly-test",
        "name": "设计部周测试页",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "modules": [
            {"key": "item", "type": "item_card", "title": "事项", "order": 1,
             "source_fields": ["title"], "aggregatable": True},
            {"key": "state", "type": "metric_card", "title": "状态", "order": 2,
             "source_fields": ["state_word"], "aggregatable": True},
            {"key": "evidence", "type": "evidence_input", "title": "依据", "order": 3,
             "source_fields": ["evidence_ref"], "aggregatable": True},
        ],
        "fields": [
            {"key": "title", "label": "事项", "type": "text", "required": True, "map_to": "title",
             "aggregatable": True, "example": "本周打样"},
            {"key": "state_word", "label": "状态", "type": "enum", "required": True, "map_to": "status",
             "aggregatable": True, "example": "进行中", "options": ["未开始", "进行中", "已完成"]},
            {"key": "evidence_ref", "label": "依据", "type": "text", "required": True, "map_to": "evidence",
             "aggregatable": True, "example": "台账 10-13"},
        ],
        "status_map": {"未开始": "planned", "进行中": "in_progress", "已完成": "done"},
        "rules": {"evidence_required": True},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }

    bad = json.loads(json.dumps(descriptor))
    bad["status_map"] = {"进行中": "not-a-status"}
    bad["interface_id"] = "design-bad-test"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    token, _ = issue_session(str(member))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                         urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path, method="GET", form=None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(base + path, data=payload, method=method)
        if payload is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(req, timeout=15) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(), dict(err.headers)

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))

    call("/login", "POST", {"token": token})
    status, form, _ = call(f"/departments/{org}/new")
    check("department member can open the create form", status == 200 and "保存并预览" in form)
    csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)

    status, _, headers = call(f"/departments/{org}/new", "POST", {
        "csrf": csrf, "slug": "design-weekly-test", "name": "设计部周测试页",
        "position_id": "", "descriptor_text": json.dumps(bad)})
    location = headers.get("location", "")
    check("bad status map is refused with a reason", status == 303 and "error=" in location, location[:160])

    status, form, _ = call(f"/departments/{org}/new")
    csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    status, _, headers = call(f"/departments/{org}/new", "POST", {
        "csrf": csrf, "slug": "design-weekly-test", "name": "设计部周测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    location = headers.get("location", "")
    check("valid page is created and opens", status == 303 and "saved=1" in location, location[:160])

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute(
            """SELECT interface_id, status FROM workhub.interfaces
                WHERE slug = 'design-weekly-test' ORDER BY version_no DESC LIMIT 1""").fetchone()
    check("new page starts as draft", row is not None and row["status"] == "draft")

    status, page, _ = call(f"/departments/{org}/{row['interface_id']}")
    check("draft page shows preview notice and trial submit", status == 200 and "保存试填" in page, status)

    status, tabbed, _ = call(f"/departments/{org}")
    if status == 302:
        status, tabbed, _ = call(f"/departments/{org}/{row['interface_id']}")
    check("department tabs list the new page", "设计部周测试页" in tabbed)


def test_department_page_creation(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
