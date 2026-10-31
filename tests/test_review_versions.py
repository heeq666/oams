"""版本审批：新旧两版并存、改动可读、退回原因可统计。

拆成两半：
* 纯逻辑部分是 `workhub.review` 的 diff 与拒绝码，不碰数据库，任何机器都能跑；
* `@pytest.mark.db` 部分用真实的 PostgreSQL 和已启动的站点，验证三条硬规则：
  1. 新版本进入待审时，线上那一版仍然是 active（旧版永不中断）；
  2. 待审页面带着基于哪一版的血缘（based_on），审核页读得出「改了什么」；
  3. 退回必须记结构化原因码；未知码被拒，一个 slug 同时只能有一版在改或待审。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from workhub import review as review_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
# 描述符快照放 tests/fixtures/：`data/live-design-*.json` 是 .gitignore 里的本机数据，
# 外面的人 clone 下来没有这个文件，模块级去读会让整个测试套件收集失败。
BASE_DESCRIPTOR = json.loads((REPO_ROOT / "tests" / "fixtures" / "live-design-interface.json").read_text(encoding="utf-8"))


def _descriptor(**overrides):
    """A copy of the live design descriptor with targeted changes."""

    copy = json.loads(json.dumps(BASE_DESCRIPTOR))
    for key, value in overrides.items():
        if value is None:
            copy.pop(key, None)
        else:
            copy[key] = value
    return copy


def _bundle(old, new):
    return review_rules.review_bundle(old, new)


def test_a_word_dropped_from_the_dropdown_is_reported_even_if_mapped():
    """The dropdown and status_map are edited in different places; a word that
    leaves only the dropdown still becomes unpickable, so the reviewer sees it."""

    narrower = _descriptor()
    fields = json.loads(json.dumps(narrower["fields"]))
    for field in fields:
        if field.get("map_to") == "status":
            field["options"] = [w for w in field["options"] if w != "等面料"]
    narrower["fields"] = fields
    bundle = _bundle(BASE_DESCRIPTOR, narrower)
    removed = [c for c in bundle["changes"] if c["kind"] == "status_removed"]
    assert len(removed) == 1
    assert removed[0]["severity"] == review_rules.SEVERITY_HIGH
    assert "等面料" in removed[0]["label"]
    assert "映射还留着" in removed[0]["why"]


def test_audience_change_is_read_from_where_descriptors_keep_it():
    """Departments write the audience under meta; a top-level-only reader would
    report every version as unchanged on the one field that decides who sees it."""

    narrower = _descriptor()
    narrower["meta"] = dict(narrower["meta"], visibility="public")
    bundle = _bundle(BASE_DESCRIPTOR, narrower)
    kinds = [c["kind"] for c in bundle["changes"]]
    assert kinds == ["visibility_changed"]
    assert bundle["changes"][0]["severity"] == review_rules.SEVERITY_HIGH
    assert bundle["changes"][0]["after"] == "public"


# --- diff: what counts as a change -------------------------------------------


def test_identical_versions_have_no_changes():
    bundle = _bundle(BASE_DESCRIPTOR, _descriptor())
    assert bundle["first_version"] is False
    assert bundle["changes"] == []
    assert bundle["summary"]["headline"] == "这一版与在线版本没有差异"
    assert bundle["summary"]["total"] == 0


def test_first_version_is_reviewed_whole_not_as_a_diff():
    bundle = _bundle(None, _descriptor())
    assert bundle["first_version"] is True
    assert bundle["changes"] == []
    assert "首版" in bundle["summary"]["headline"]


def test_renaming_a_field_is_only_a_hint():
    new = _descriptor()
    for field in new["fields"]:
        if field["key"] == "note_text":
            field["label"] = "备注说明"
    changes = _bundle(BASE_DESCRIPTOR, new)["changes"]
    kinds = {change["kind"] for change in changes}
    assert kinds == {"field_label_changed"}
    assert changes[0]["severity"] == review_rules.SEVERITY_LOW


def test_remapping_a_field_changes_the_meaning_of_the_numbers():
    new = _descriptor()
    for field in new["fields"]:
        if field["key"] == "sample_done":
            field["map_to"] = "note"
    bundle = _bundle(BASE_DESCRIPTOR, new)
    high = [change for change in bundle["changes"] if change["severity"] == review_rules.SEVERITY_HIGH]
    assert [change["kind"] for change in high] == ["field_mapping_changed"]
    assert high[0]["before"] == "progress_value"
    assert high[0]["after"] == "note"
    assert bundle["summary"]["has_high"] is True


def test_dropping_a_status_word_is_high_because_old_entries_fall_out():
    new = _descriptor(status_map={"待打样": "planned", "打样中": "in_progress", "已完成": "done"})
    changes = _bundle(BASE_DESCRIPTOR, new)["changes"]
    assert [change["kind"] for change in changes] == ["status_removed"]
    assert changes[0]["severity"] == review_rules.SEVERITY_HIGH
    assert "等面料" in changes[0]["label"]


def test_adding_a_capability_is_high_but_removing_one_is_a_hint():
    more = _descriptor(capabilities=["entries:write", "entries:read:dept", "entries:read:company"])
    added = _bundle(BASE_DESCRIPTOR, more)["changes"]
    assert [(change["kind"], change["severity"]) for change in added] == [
        ("capability_added", review_rules.SEVERITY_HIGH)]

    fewer = _descriptor(capabilities=["entries:write"])
    removed = _bundle(BASE_DESCRIPTOR, fewer)["changes"]
    assert [(change["kind"], change["severity"]) for change in removed] == [
        ("capability_removed", review_rules.SEVERITY_LOW)]


def test_retiring_a_metric_is_high_and_explains_why():
    new = _descriptor(metrics=[])
    changes = _bundle(BASE_DESCRIPTOR, new)["changes"]
    assert [change["kind"] for change in changes] == ["metric_removed"]
    assert changes[0]["severity"] == review_rules.SEVERITY_HIGH
    assert changes[0]["why"]


def test_changes_come_back_worst_first():
    new = _descriptor(status_map={"待打样": "planned", "打样中": "in_progress"})
    new["fields"][-1]["label"] = "备注（新的叫法）"
    severities = [change["severity"] for change in _bundle(BASE_DESCRIPTOR, new)["changes"]]
    order = [review_rules.SEVERITY_ORDER[severity] for severity in severities]
    assert order == sorted(order)
    assert severities[0] == review_rules.SEVERITY_HIGH


def test_headline_counts_the_costly_changes():
    new = _descriptor(status_map={"待打样": "planned"}, metrics=[])
    summary = _bundle(BASE_DESCRIPTOR, new)["summary"]
    assert summary["high"] == 4  # three status words dropped, one metric retired
    assert summary["headline"].startswith("4 处会改变公司数字的含义")
    assert summary["areas"][0] == {"area": review_rules.AREA_STATUS, "count": 3}


def test_describe_changes_is_short_enough_for_an_audit_reason():
    new = _descriptor(status_map={}, metrics=[])
    text = review_rules.describe_changes(_bundle(BASE_DESCRIPTOR, new)["changes"], limit=3)
    assert text.count("；") <= 2
    assert "[需注意]" in text


# --- rejection codes --------------------------------------------------------


def test_documented_rejection_codes_are_all_reachable():
    codes = [code for code, _label, _hint in review_rules.REJECTION_CODES]
    assert len(codes) == len(set(codes)) == 7
    assert set(codes) == set(review_rules.REJECTION_LABELS)
    for code in codes:
        assert review_rules.is_rejection_code(code)
        assert review_rules.rejection_label(code) != code
    for choice in review_rules.REJECTION_CHOICES:
        assert choice["label"] and choice["hint"]


def test_unknown_and_empty_codes_stay_readable_but_are_not_valid():
    assert review_rules.rejection_label(None) == "未分类"
    assert review_rules.rejection_label("") == "未分类"
    assert review_rules.rejection_label("LEGACY_CODE") == "LEGACY_CODE"
    assert review_rules.is_rejection_code("LEGACY_CODE") is False
    assert review_rules.is_rejection_code(None) is False


# --- database: two versions side by side ------------------------------------

pytestmark_db = pytest.mark.db

PROBE_SLUG = "review-versions-probe"


def _no_redirect():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return NoRedirect()


def _session(base: str, token: str):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _no_redirect(), urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path: str, method: str = "GET", form: dict | None = None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(base + path, data=payload, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), dict(error.headers)

    call("/login", "POST", {"token": token})
    return call


def _token_of(html: str, name: str = "csrf") -> str:
    match = re.search(r'name="' + name + r'"\s+value="([^"]+)"', html)
    assert match, "form token " + name + " missing from page"
    return match.group(1)


@pytest.mark.db
def test_versions_side_by_side(site) -> None:
    import psycopg
    from psycopg.rows import dict_row

    from workhub.security import issue_session

    base = os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000")
    url = os.environ["WORKHUB_DATABASE_URL"]
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
        if not ok:
            failures.append(name)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        admin = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'ADMIN001'").fetchone()["member_id"]
        # The reviewer is somebody other than the author on purpose: a page is
        # reviewed by a second pair of eyes or it is not reviewed at all, and the
        # queue refuses to show a reviewer their own submission.
        reviewer_member = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'DESIGN001'").fetchone()["member_id"]
        org = conn.execute(
            "SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'DESIGN'").fetchone()["org_unit_id"]
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = %s", (PROBE_SLUG,))
        conn.execute("DELETE FROM workhub.interfaces WHERE slug = %s", (PROBE_SLUG + "-other",))
        live = conn.execute(
            """INSERT INTO workhub.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    version_no, created_by, updated_by)
               VALUES (%s, %s, %s, 'active', 'department', '0.2', %s, 1, %s, %s)
               RETURNING interface_id""",
            (org, PROBE_SLUG, "版本审批探针", json.dumps(BASE_DESCRIPTOR), admin, admin)).fetchone()["interface_id"]
        next_descriptor = json.loads(json.dumps(BASE_DESCRIPTOR))
        next_descriptor["status_map"] = {"待打样": "planned", "打样中": "in_progress", "已完成": "done"}

    try:
        token, _ = issue_session(str(admin))
        call = _session(base, token)
        review_token, _ = issue_session(str(reviewer_member))
        review = _session(base, review_token)

        # The department writes the next version through the real form, because
        # the rule being tested lives in the handler: creating a draft used to
        # archive the live version, blanking the department's page for as long
        # as the review took -- and forever if the draft was abandoned.
        status, form, _ = call(f"/departments/{org}/new")
        check("the new-page form opens", status == 200, status)
        check("the form has a description field", 'name="descriptor_text"' in form)
        status, _, headers = call(f"/departments/{org}/new", "POST", {
            "csrf": _token_of(form),
            "slug": PROBE_SLUG,
            "name": "版本审批探针",
            "position_id": "",
            "descriptor_text": json.dumps(next_descriptor, ensure_ascii=False),
        })
        location = headers.get("location", "")
        check("a second version is accepted", status == 303 and "saved=1" in location,
              location[:160] or form[:200])

        with psycopg.connect(url, row_factory=dict_row) as conn:
            versions = conn.execute(
                """SELECT interface_id, version_no, status, based_on FROM workhub.interfaces
                   WHERE org_unit_id = %s AND slug = %s ORDER BY version_no""",
                (org, PROBE_SLUG)).fetchall()
        check("two versions now exist", [row["version_no"] for row in versions] == [1, 2], versions)
        draft = versions[-1]["interface_id"] if len(versions) == 2 else versions[0]["interface_id"]
        check("the live version was not taken offline", versions[0]["status"] == "active",
              versions[0]["status"])
        check("the draft records which version it was based on", str(versions[-1]["based_on"]) == str(live),
              versions[-1]["based_on"])

        # One open version at a time: a second draft would give the reviewer two
        # diffs against one live version and no way to tell which one ships.
        status, _, headers = call(f"/departments/{org}/new", "POST", {
            "csrf": _token_of(form),
            "slug": PROBE_SLUG,
            "name": "版本审批探针",
            "position_id": "",
            "descriptor_text": json.dumps(BASE_DESCRIPTOR, ensure_ascii=False),
        })
        check("a second open version of the same page is refused",
              status == 303 and "error=" in headers.get("location", ""),
              headers.get("location", "")[:160])

        status, page, _ = call(f"/departments/{org}/{draft}")
        check("the workbench page opens for the draft", status == 200, status)
        status, _, headers = call(f"/departments/{org}/{draft}/submit-review", "POST",
                                  {"review_csrf": _token_of(page, "review_csrf")})
        check("the department can send it for review",
              status == 303 and "saved=review" in headers.get("location", ""),
              headers.get("location", "")[:160])

        status, html, _ = review("/reviews")
        check("review queue opens", status == 200, status)
        check("queue reads the change, not the whole page", "这一版改了什么" in html)
        check("high change is called out", "需注意" in html)
        check("the change names the status word that was dropped", "等面料" in html)
        check("the diff says which live version it compared", "v1" in html)
        check("a high change warns about history", "历史数据不会重算" in html)

        with psycopg.connect(url, row_factory=dict_row) as conn:
            live_status = conn.execute(
                "SELECT status FROM workhub.interfaces WHERE interface_id = %s", (live,)).fetchone()["status"]
        check("the live version keeps serving while the new one waits", live_status == "active", live_status)

        csrf = _token_of(html)
        status, _, headers = review(f"/reviews/{draft}/reject", "POST",
                                  {"csrf": csrf, "note": "状态词删得没道理", "reason_code": "NOT_A_CODE"})
        location = headers.get("location", "")
        check("an unknown rejection code is refused", status == 303 and "error=" in location, location[:120])
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT status, review_code FROM workhub.interfaces WHERE interface_id = %s", (draft,)).fetchone()
        check("the refused decision changed nothing", row["status"] == "pending" and row["review_code"] is None,
              dict(row))

        status, _, headers = review(f"/reviews/{draft}/reject", "POST",
                                  {"csrf": csrf, "note": "状态词删得没道理",
                                   "reason_code": "STATUS_UNMAPPED"})
        check("a documented code is accepted", status == 303 and "error=" not in headers.get("location", ""),
              headers.get("location", "")[:120])
        with psycopg.connect(url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT status, review_code, review_note FROM workhub.interfaces WHERE interface_id = %s",
                (draft,)).fetchone()
        check("the page goes back to draft", row["status"] == "draft", row["status"])
        check("the reason code is recorded", row["review_code"] == "STATUS_UNMAPPED", row["review_code"])

        status, html, _ = review("/reviews")
        check("the decision list shows the reason by name", "状态词没有映射" in html)

        # A department editing v2 must know the company still reads v1.
        status, page, _ = call(f"/departments/{org}/{draft}")
        check("the workbench page opens for the draft", status == 200, status)
        check("it names the version the company still reads", "公司现在看到的是" in page)
        check("it shows the rejection code by name", "状态词没有映射" in page or "退回原因类型" in page)
    finally:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            conn.execute("DELETE FROM workhub.interfaces WHERE slug IN (%s, %s)",
                         (PROBE_SLUG, PROBE_SLUG + "-other"))

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all version-review checks passed")
