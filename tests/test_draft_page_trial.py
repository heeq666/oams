"""A page under review can be trial-filled by its own department and stays out of the company aggregate.

Self-contained: it creates its own draft page instead of relying on whatever
happens to be in the database, so the check keeps meaning the same thing as the
data changes.
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
    from psycopg.rows import dict_row
    from workhub.security import issue_session

    base = os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000")
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
        if not ok:
            failures.append(name)

    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-draft-trial",
        "name": "设计部试填隔离测试页",
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
             "aggregatable": True, "example": "试填隔离"},
            {"key": "state_word", "label": "状态", "type": "enum", "required": True, "map_to": "status",
             "aggregatable": True, "example": "进行中", "options": ["未开始", "进行中", "已完成"]},
            {"key": "evidence_ref", "label": "依据", "type": "text", "required": True, "map_to": "evidence",
             "aggregatable": True, "example": "T-001"},
        ],
        "status_map": {"未开始": "planned", "进行中": "in_progress", "已完成": "done"},
        "rules": {"evidence_required": True},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }
    # 0.1 descriptors are legal at creation time but no longer render: the renderer
    # only knows the 0.2 module whitelist.  Upgrade first, so these scripts keep
    # testing the flow instead of the migration.
    descriptor, _ = upgrade_descriptor(descriptor)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        member = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code='DESIGN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = 'design-draft-trial'")

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

    def csrf_of(html, field="csrf"):
        return re.search('name="' + field + '" value="([^"]+)"', html).group(1)

    call("/login", "POST", {"token": token})
    status, form, _ = call(f"/departments/{org}/new")
    check("create form opens", status == 200 and "保存并预览" in form, status)
    status, _, headers = call(f"/departments/{org}/new", "POST", {
        "csrf": csrf_of(form), "slug": "design-draft-trial", "name": "设计部试填隔离测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    check("draft page created", status == 303 and "saved=1" in headers.get("location", ""), headers.get("location"))

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        page = conn.execute("SELECT interface_id, status FROM workhub.interfaces WHERE slug='design-draft-trial'").fetchone()
    check("new page is a draft", page["status"] == "draft", page)

    status, html, _ = call(f"/departments/{org}/{page['interface_id']}")
    check("draft page opens for the department", status == 200 and "保存试填" in html, status)

    status, _, headers = call(f"/departments/{org}/{page['interface_id']}", "POST", {
        "csrf": csrf_of(html), "title": "测试页面-试填", "state_word": "进行中", "evidence_ref": "测试依据 T-001"})
    check("draft page accepts a trial entry", status == 303 and "saved=1" in headers.get("location", ""),
          headers.get("location"))

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute(
            """SELECT entry_mode FROM workhub.interface_entries
                WHERE interface_id = %s AND title = '测试页面-试填'""", (page["interface_id"],)).fetchone()
        live_count = conn.execute(
            """SELECT count(*) AS n FROM workhub.interface_entries
                WHERE interface_id = %s AND entry_mode = 'live'""", (page["interface_id"],)).fetchone()["n"]
    check("trial entry stored in draft mode", row is not None and row["entry_mode"] == "draft", row)
    check("no trial row is live", live_count == 0, live_count)

    status, board, _ = call("/interfaces/board")
    check("manager board does not show the trial entry", "测试页面-试填" not in board)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("DELETE FROM workhub.interfaces WHERE slug='design-draft-trial'")

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all draft trial checks passed")


def test_draft_page_trial(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
