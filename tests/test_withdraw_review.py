"""A department can take back a review request that nobody has decided yet."""
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
    """Walk the whole withdraw story through the served app, one real request at a time.

    Everything is produced by the product: a page is created, submitted, withdrawn,
    submitted again and returned by a reviewer.  The one state worth naming is
    "waiting for review *and* already carrying a decision" -- that comes from
    returning a page and sending it again, not from writing the columns by hand.
    """

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
        "interface_id": "design-withdraw-flow",
        "name": "设计部撤回测试页",
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
             "aggregatable": True, "example": "撤回测试"},
            {"key": "state_word", "label": "状态", "type": "enum", "required": True, "map_to": "status",
             "aggregatable": True, "example": "进行中", "options": ["进行中", "已完成"]},
            {"key": "evidence_ref", "label": "依据", "type": "text", "required": True, "map_to": "evidence",
             "aggregatable": True, "example": "W-1"},
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
        admin = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code='ADMIN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = 'design-withdraw-flow'")

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

    def token_of(html, field="review_csrf"):
        return re.search('name="' + field + '" value="([^"]+)"', html).group(1)

    design_call = session(design)
    admin_call = session(admin)

    status, form, _ = design_call(f"/departments/{org}/new")
    status, _, headers = design_call(f"/departments/{org}/new", "POST", {
        "csrf": token_of(form, "csrf"), "slug": "design-withdraw-flow", "name": "设计部撤回测试页",
        "position_id": "", "descriptor_text": json.dumps(descriptor)})
    check("page created", status == 303 and "saved=1" in headers.get("location", ""), headers.get("location"))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        page = conn.execute("SELECT interface_id FROM workhub.interfaces WHERE slug='design-withdraw-flow'").fetchone()
    iid = page["interface_id"]

    status, html, _ = design_call(f"/departments/{org}/{iid}")
    check("withdraw button is absent while still a draft", "撤回提交" not in html)

    status, _, headers = design_call(f"/departments/{org}/{iid}/submit-review", "POST",
                                     {"review_csrf": token_of(html)})
    check("submitted for review", status == 303 and "saved=review" in headers.get("location", ""),
          headers.get("location"))

    status, html, _ = design_call(f"/departments/{org}/{iid}")
    check("withdraw button appears while pending", "撤回提交" in html)

    status, _, headers = design_call(f"/departments/{org}/{iid}/withdraw-review", "POST",
                                     {"review_csrf": token_of(html)})
    check("withdraw accepted", status == 303 and "saved=withdrawn" in headers.get("location", ""),
          headers.get("location"))
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        state = conn.execute("SELECT status FROM workhub.interfaces WHERE interface_id=%s", (iid,)).fetchone()["status"]
    check("page is back to draft", state == "draft", state)

    status, queue, _ = admin_call("/reviews")
    check("reviewer no longer sees it", "设计部撤回测试页" not in queue)

    # A decided page cannot be withdrawn: the decision is on record.
    status, html, _ = design_call(f"/departments/{org}/{iid}")
    design_call(f"/departments/{org}/{iid}/submit-review", "POST", {"review_csrf": token_of(html)})
    status, queue, _ = admin_call("/reviews")
    admin_call(f"/reviews/{iid}/reject", "POST", {"csrf": token_of(queue, "csrf"), "note": "再改改"})
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, reviewed_at FROM workhub.interfaces WHERE interface_id=%s", (iid,)).fetchone()
    check("rejected page returns to draft with a decision on record",
          row["status"] == "draft" and row["reviewed_at"] is not None, row)

    # A page that was returned once and then sent again is the real way to get a
    # *waiting page that already carries a decision*: submit only flips the status
    # back to pending, it does not clear the returned decision.  So this state is
    # produced by the product itself -- no direct UPDATE is needed (and a direct
    # UPDATE would test a state nobody can actually reach).
    status, html, _ = design_call(f"/departments/{org}/{iid}")
    review_token = token_of(html, "review_csrf")  # the draft page's submit form carries it
    design_call(f"/departments/{org}/{iid}/submit-review", "POST", {"review_csrf": review_token})
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        again = conn.execute("SELECT status, reviewed_at FROM workhub.interfaces WHERE interface_id=%s",
                             (iid,)).fetchone()
    check("re-submitted page waits again but still carries the returned decision",
          again["status"] == "pending" and again["reviewed_at"] is not None, again)
    status, html, _ = design_call(f"/departments/{org}/{iid}")
    check("withdraw button hidden once a decision exists", "撤回提交" not in html)
    # The endpoint must refuse it even when the button is gone: the guard cannot live
    # in the template.  Reuse the page-form token taken from the draft page above --
    # it binds to the session, not to a page.  (Grabbing the other ``csrf`` field out
    # of this HTML would only prove that a wrong-purpose token gets a 403, which is a
    # different guard entirely.)
    status, _, headers = design_call(f"/departments/{org}/{iid}/withdraw-review", "POST",
                                     {"review_csrf": review_token})
    location = headers.get("location", "")
    # 303 is what *both* outcomes return, so the status alone proves nothing: a
    # successful withdraw would also be a 303 -- only the destination tells them apart.
    check("withdraw refused after a decision",
          status == 303 and "saved=withdrawn" not in location, (status, location[:120]))
    reason = urllib.parse.unquote(location)
    check("refusal explains why", "error=" in reason and "审核已给出意见" in reason, reason[:160])
    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        still = conn.execute("SELECT status FROM workhub.interfaces WHERE interface_id=%s", (iid,)).fetchone()["status"]
    check("page stayed pending after the refused withdraw", still == "pending", still)

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("DELETE FROM workhub.interfaces WHERE slug='design-withdraw-flow'")

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all withdraw checks passed")


def test_withdraw_review(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
