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

from oams.upgrade import upgrade_descriptor  # noqa: E402


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from psycopg.rows import dict_row
    from oams.security import issue_session

    base = os.environ.get("OAMS_BASE", "http://127.0.0.1:8000")
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

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"], row_factory=dict_row) as conn:
        design = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code='DESIGN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
        # The page has to be written by somebody other than its reviewer: a page
        # nobody else looked at is not a reviewed page, and the queue will not
        # offer a reviewer their own submission.  A second person takes a DESIGN
        # seat for the length of this script, and gives it back in the cleanup.
        writer = conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code='MVP1001'").fetchone()["member_id"]
        design_position = conn.execute(
            "SELECT position_id FROM oams.positions WHERE position_code='DESIGN_LEAD'").fetchone()["position_id"]
        conn.execute("DELETE FROM oams.position_assignments WHERE member_id=%s AND position_id=%s",
                     (writer, design_position))
        conn.execute(
            """INSERT INTO oams.position_assignments
                   (member_id, position_id, assignment_type, status, starts_at, reason, created_by)
               VALUES (%s, %s, 'concurrent', 'active', now() - interval '1 day',
                       '权限拆分用例：临时给第二个设计部席位', %s)""",
            (writer, design_position, design))
        # This member must NOT be a security administrator: that is the whole point.
        grants_admin = conn.execute(
            """SELECT count(*) AS n FROM oams.permission_grants
                WHERE resource = 'workforce.grants' AND action = 'manage' AND revoked_at IS NULL
                  AND (member_id = %s OR position_id IN (
                        SELECT position_id FROM oams.position_assignments WHERE member_id = %s))""",
            (design, design)).fetchone()["n"]
        conn.execute("DELETE FROM oams.interfaces WHERE slug = 'design-permission-split'")
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

    # Cannot do administrator work. A page they may not read is not an error
    # screen: they are sent on to one they can read, with the reason in a short
    # notice cookie (the POST below still has to explain itself in place).
    def location(headers):
        return next((v for k, v in headers.items() if k.lower() == "location"), "")

    status, body, headers = lead("/setup")
    check("reviewer is steered away from the guided setup page",
          status == 303 and "没有权限" not in body, f"{status} {location(headers)}")
    status, body, headers = lead("/admin/audit")
    check("reviewer is steered away from the audit log",
          status == 303 and "没有权限" not in body, f"{status} {location(headers)}")

    # Full review cycle: the department writes and sends, this non-administrator
    # reviewer signs. The writer is a second person because a reviewer cannot
    # approve the version they wrote themselves.
    draft = session(writer)
    status, form, _ = draft(f"/departments/{org}/new")
    status, _, headers = draft(f"/departments/{org}/new", "POST", {
        "csrf": token_of(form), "slug": "design-permission-split", "name": "设计部权限拆分测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    check("page created", status == 303 and "saved=1" in headers.get("location", ""), headers.get("location"))

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"], row_factory=dict_row) as conn:
        iid = conn.execute("SELECT interface_id FROM oams.interfaces WHERE slug='design-permission-split'").fetchone()["interface_id"]

    status, html, _ = draft(f"/departments/{org}/{iid}")
    status, _, headers = draft(f"/departments/{org}/{iid}/submit-review", "POST",
                               {"review_csrf": token_of(html, "review_csrf")})
    check("submitted for review", status == 303 and "saved=review" in headers.get("location", ""),
          headers.get("location"))

    status, queue, _ = lead("/reviews")
    check("reviewer sees the submission", "设计部权限拆分测试页" in queue, status)
    status, _, headers = lead(f"/reviews/{iid}/approve", "POST",
                              {"csrf": token_of(queue), "note": "权限拆分验证"})
    check("non-administrator reviewer can approve", status == 303 and "saved=approved" in headers.get("location", ""),
          headers.get("location", "")[:160])

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, reviewed_by FROM oams.interfaces WHERE interface_id=%s", (iid,)).fetchone()
        conn.execute("DELETE FROM oams.interfaces WHERE slug='design-permission-split'")
        conn.execute("DELETE FROM oams.position_assignments WHERE member_id=%s AND position_id=%s",
                     (writer, design_position))
    check("approval recorded the reviewer", row["status"] == "active" and row["reviewed_by"] == design, row)

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all review permission checks passed")


def test_review_permission(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
