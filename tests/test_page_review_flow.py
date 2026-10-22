"""The full page lifecycle: build -> submit for review -> approve -> live and aggregated."""
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
        "interface_id": "design-review-flow",
        "name": "设计部审核流程测试页",
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
             "aggregatable": True, "example": "审核流程测试"},
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
    # 0.1 descriptors are legal at creation time but no longer render: the renderer
    # only knows the 0.2 module whitelist.  Upgrade first, so these scripts keep
    # testing the flow instead of the migration.
    descriptor, _ = upgrade_descriptor(descriptor)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        design = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code='DESIGN001'").fetchone()["member_id"]
        admin = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code='ADMIN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = 'design-review-flow'")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def session(member_id):
        token, _ = issue_session(str(member_id))
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

        call("/login", "POST", {"token": token})
        return call

    def csrf_of(html):
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    design_call = session(design)
    admin_call = session(admin)

    # 1. Department builds a page: it starts as a draft.
    status, form, _ = design_call(f"/departments/{org}/new")
    check("department opens the create form", status == 200 and "保存并预览" in form)
    status, _, headers = design_call(f"/departments/{org}/new", "POST", {
        "csrf": csrf_of(form), "slug": "design-review-flow", "name": "设计部审核流程测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    check("page created", status == 303 and "saved=1" in headers.get("location", ""), headers.get("location"))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        page = conn.execute("SELECT interface_id, status FROM workhub.interfaces WHERE slug='design-review-flow'").fetchone()
    check("starts as draft", page["status"] == "draft", page)

    # 2. Department can still trial-fill while building.
    status, html, _ = design_call(f"/departments/{org}/{page['interface_id']}")
    check("draft page is fillable", status == 200 and "保存试填" in html)
    status, _, headers = design_call(f"/departments/{org}/{page['interface_id']}", "POST", {
        "csrf": csrf_of(html), "title": "试填记录", "state_word": "进行中", "evidence_ref": "T-1"})
    check("trial fill accepted", status == 303 and "saved=1" in headers.get("location", ""))

    # 3. Department submits for review.
    status, html, _ = design_call(f"/departments/{org}/{page['interface_id']}")
    check("submit-for-review button is shown", "提交审核" in html)
    review_token = re.search(r'name="review_csrf" value="([^"]+)"', html).group(1)
    status, _, headers = design_call(
        f"/departments/{org}/{page['interface_id']}/submit-review", "POST", {"review_csrf": review_token})
    check("review request accepted", status == 303 and "saved=review" in headers.get("location", ""),
          headers.get("location"))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        state = conn.execute("SELECT status FROM workhub.interfaces WHERE interface_id=%s", (page["interface_id"],)).fetchone()["status"]
    check("page is now pending", state == "pending", state)

    # 4. Reviewer sees it in the queue.
    status, queue, _ = admin_call("/reviews")
    check("reviewer sees the pending page", status == 200 and "设计部审核流程测试页" in queue, status)

    # 5. Department cannot approve its own page.
    status, _, _ = design_call(f"/reviews/{page['interface_id']}/approve", "POST", {"csrf": csrf_of(html)})
    check("department cannot approve its own page", status == 403, status)

    # 6. Reviewer rejects with a reason: back to draft, note recorded.
    status, _, headers = admin_call(f"/reviews/{page['interface_id']}/reject", "POST",
                                    {"csrf": csrf_of(queue), "note": "字段太少，补充卡点"})
    check("reject accepted", status == 303 and "saved=rejected" in headers.get("location", ""))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, review_note FROM workhub.interfaces WHERE interface_id=%s",
                           (page["interface_id"],)).fetchone()
    check("reject returns to draft with the note", row["status"] == "draft" and "字段太少" in (row["review_note"] or ""), row)

    status, html, _ = design_call(f"/departments/{org}/{page['interface_id']}")
    check("department sees the review note", "字段太少，补充卡点" in html)

    # 7. Resubmit, then approve: becomes live and only then enters the aggregate.
    status, _, _ = design_call(f"/departments/{org}/{page['interface_id']}/submit-review", "POST",
                              {"review_csrf": re.search(r'name="review_csrf" value="([^"]+)"', html).group(1)})
    status, queue, _ = admin_call("/reviews")
    status, _, headers = admin_call(f"/reviews/{page['interface_id']}/approve", "POST",
                                    {"csrf": csrf_of(queue), "note": "通过"})
    check("approve accepted", status == 303 and "saved=approved" in headers.get("location", ""))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute(
            """SELECT status, reviewed_by, reviewed_at FROM workhub.interfaces WHERE interface_id=%s""",
            (page["interface_id"],)).fetchone()
    check("page is active with a recorded reviewer",
          row["status"] == "active" and row["reviewed_by"] == admin and row["reviewed_at"] is not None, row)

    # 8. Trial entries made before approval kept their draft mode and never aggregated.
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        modes = conn.execute(
            "SELECT entry_mode, count(*) AS n FROM workhub.interface_entries WHERE interface_id=%s GROUP BY 1",
            (page["interface_id"],)).fetchall()
    modes = {r["entry_mode"]: r["n"] for r in modes}
    check("pre-approval trial stayed in draft mode", modes.get("draft", 0) >= 1 and modes.get("live", 0) == 0, modes)

    # 9. After approval a real entry goes live.
    status, html, _ = design_call(f"/departments/{org}/{page['interface_id']}")
    status, _, headers = design_call(f"/departments/{org}/{page['interface_id']}", "POST", {
        "csrf": csrf_of(html), "title": "正式记录", "state_word": "进行中", "evidence_ref": "L-1"})
    check("approved page accepts a live entry", status == 303 and "saved=1" in headers.get("location", ""))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        live = conn.execute(
            """SELECT count(*) AS n FROM workhub.interface_entries
                WHERE interface_id=%s AND entry_mode='live'""", (page["interface_id"],)).fetchone()["n"]
    check("live entry recorded", live == 1, live)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("DELETE FROM workhub.interfaces WHERE slug='design-review-flow'")

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all page lifecycle checks passed")


def test_page_review_flow(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
