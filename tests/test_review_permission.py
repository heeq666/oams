"""Reviewing pages is its own permission: a reviewer is not a security administrator."""
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
        "interface_id": "design-permission-split",
        "name": "设计部权限拆分测试页",
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
             "aggregatable": True, "example": "权限拆分"},
            {"key": "state_word", "label": "状态", "type": "enum", "required": True, "map_to": "status",
             "aggregatable": True, "example": "进行中", "options": ["进行中", "已完成"]},
            {"key": "evidence_ref", "label": "依据", "type": "text", "required": True, "map_to": "evidence",
             "aggregatable": True, "example": "P-1"},
        ],
        "status_map": {"进行中": "in_progress", "已完成": "done"},
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
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
        # This member must NOT be a security administrator: that is the whole point.
        grants_admin = conn.execute(
            """SELECT count(*) AS n FROM workhub.permission_grants
                WHERE resource = 'workforce.grants' AND action = 'manage' AND revoked_at IS NULL
                  AND (member_id = %s OR position_id IN (
                        SELECT position_id FROM workhub.position_assignments WHERE member_id = %s))""",
            (design, design)).fetchone()["n"]
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = 'design-permission-split'")
    check("design lead is not a security administrator", grants_admin == 0, grants_admin)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def session(member_id):
        token, _ = issue_session(str(member_id))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                             urllib.request.HTTPCookieProcessor(CookieJar()))

        def call(path, method="GET", form=None, follow=False):
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

    def token_of(html, field="csrf"):
        return re.search('name="' + field + '" value="([^"]+)"', html).group(1)

    lead = session(design)

    # Can review.
    status, page, _ = lead("/reviews")
    check("reviewer permission opens the review queue", status == 200 and "页面审核" in page, status)

    # Cannot do administrator work.
    status, _, _ = lead("/setup")
    check("reviewer cannot open the guided setup page", status == 403, status)
    status, _, _ = lead("/admin/audit")
    check("reviewer cannot read the audit log", status == 403, status)

    # Full review cycle driven by this non-administrator reviewer.
    status, form, _ = lead(f"/departments/{org}/new")
    status, _, headers = lead(f"/departments/{org}/new", "POST", {
        "csrf": token_of(form), "slug": "design-permission-split", "name": "设计部权限拆分测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    check("page created", status == 303 and "saved=1" in headers.get("location", ""), headers.get("location"))

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        iid = conn.execute("SELECT interface_id FROM workhub.interfaces WHERE slug='design-permission-split'").fetchone()["interface_id"]

    status, html, _ = lead(f"/departments/{org}/{iid}")
    status, _, headers = lead(f"/departments/{org}/{iid}/submit-review", "POST",
                              {"review_csrf": token_of(html, "review_csrf")})
    check("submitted for review", status == 303 and "saved=review" in headers.get("location", ""),
          headers.get("location"))

    status, queue, _ = lead("/reviews")
    check("reviewer sees the submission", "设计部权限拆分测试页" in queue, status)
    status, _, headers = lead(f"/reviews/{iid}/approve", "POST",
                              {"csrf": token_of(queue), "note": "权限拆分验证"})
    check("non-administrator reviewer can approve", status == 303 and "saved=approved" in headers.get("location", ""),
          headers.get("location", "")[:160])

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, reviewed_by FROM workhub.interfaces WHERE interface_id=%s", (iid,)).fetchone()
        conn.execute("DELETE FROM workhub.interfaces WHERE slug='design-permission-split'")
    check("approval recorded the reviewer", row["status"] == "active" and row["reviewed_by"] == design, row)

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all review permission checks passed")


def test_review_permission(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
