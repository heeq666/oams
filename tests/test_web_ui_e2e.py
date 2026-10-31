"""End-to-end check of the server-rendered UI: login cookie, board, admin pages.

It needs a throwaway database that has already been seeded, because it signs in as
the seeded administrator and as the seeded specialist:

    docker run -d --name workhub-web-test \
        -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=workhub_test \
        -p 55433:5432 postgres:16-alpine
    export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@localhost:55433/workhub_test
    python -m workhub.cli migrate
    ADMIN=$(python -m workhub.cli bootstrap-admin --name Admin --employee-code 9999 \
        --org-code acme --org-name ACME | python -c "import json,sys;print(json.load(sys.stdin)['member_id'])")
    WORKHUB_MIGRATOR_URL=$WORKHUB_DATABASE_URL WORKHUB_ADMIN_MEMBER_ID=$ADMIN \
        python scripts/load_department_data.py data/seed-hr-department.json --apply
    python tests/test_web_ui_e2e.py

Exit code 0 means every assertion passed. The checks that matter most are the
negative ones: an unauthenticated board redirects, and the specialist -- who has
no audit grant -- is refused the audit page rather than shown a thinner version
of it.
"""
import http.cookiejar
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])
# The conftest fixture serves the app on a free port and points WORKHUB_BASE at
# it; BASE is rebound per test inside _script().
BASE = os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000")
results = []


def check(label, passed, detail=""):
    results.append((label, passed, detail))
    print(("PASS  " if passed else "FAIL  ") + label + (f"  [{detail}]" if detail else ""), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def strip_counts(html):
    """{stage id: count printed on the flow strip} -- the strip is one row of
    counters, so anything outside it does not count as a flow number."""
    block = re.search(r'<div class="flowstrip">([\s\S]*?)</div>', html)
    out = {}
    if not block:
        return out
    # The backreference matters: each pill is wrapped in its own <a> or <span>, so
    # matching lazily to the first closing tag would stop inside the counter span.
    for _, stage_id, inner in re.findall(r'<(a|span) class="stage (\w+)[^"]*"[^>]*>([\s\S]*?)</\1>', block.group(1)):
        found = re.search(r'class="cnt">(\d+)', inner)
        out[stage_id] = int(found.group(1)) if found else None
    return out


def listed_cards(html):
    """Every card painted in the one list under the strip."""
    return html.count('<li class="')


def api_json(path):
    """The JSON API wants a bearer header; the cookie is only good for the pages.
    Comparing the two is the point: the layer badge and the lane must agree."""
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + admin_token})
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def hdr(headers, name):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def call(path, method="GET", body=None, form=None):
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def page_token(path, field="csrf"):
    """The form token a page hands out; admin POSTs are refused without it."""

    code, body, _ = call(path)
    assert code == 200, (path, code)
    found = re.search('name="' + field + '" value="([^"]+)"', body)
    assert found, f"no {field} on {path}"
    return found.group(1)

def session_form_token(purpose="workhub:form:v1"):
    """The form token for whatever account the opener is signed in as.

    An account without the manage grant never sees the form, so the token cannot
    be read off a page -- yet a request carrying a *valid* token is exactly what
    proves the refusal comes from the grant and not from a missing field.
    """

    jar = next(h.cookiejar for h in opener.handlers if hasattr(h, "cookiejar"))
    cookie = next((c for c in jar if c.name == "workhub_session"), None)
    assert cookie is not None and cookie.value, (
        "the opener is not signed in, so no form token can be derived; "
        f"cookies={[c.name for c in jar]}"
    )
    return hmac.new(cookie.value.encode(), purpose.encode(), hashlib.sha256).hexdigest()

pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script(base: str) -> None:
    """Body of the original standalone script, against a served seed-hr database."""

    global BASE, admin_token, opener
    BASE = base
    sys.path.insert(0, ROOT + "/app")
    from workhub.security import issue_session  # noqa: E402
    import psycopg  # noqa: E402
    from psycopg.rows import dict_row  # noqa: E402

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        people = {row["employee_code"]: row["member_id"]
                  for row in conn.execute("SELECT employee_code, member_id FROM workhub.workforce_members").fetchall()}


    code, body, headers = call("/board")
    check("board without cookie redirects to login",
          code == 302 and hdr(headers, "Location") or "".startswith("/login"), f"{code} {headers.get('Location')}")

    code, body, headers = call("/login?token=not-a-real-token")
    check("bad token does not sign in", code == 200 and "令牌无效" in body, str(code))

    admin_token, _ = issue_session(people["9999"])
    code, body, headers = call(f"/login?token={admin_token}")
    cookie = hdr(headers, "Set-Cookie") or ""
    check("login link sets cookie and redirects to a clean URL",
          code == 302 and "workhub_session=" in cookie and hdr(headers, "Location") == "/board",
          f"{code} {headers.get('Location')}")
    check("cookie is HttpOnly and SameSite=Lax without Secure",
          "httponly" in cookie.lower() and "samesite=lax" in cookie.lower() and "secure" not in cookie.lower(), cookie[:160])

    code, body, _ = call("/board")
    check("board renders for the admin", code == 200 and "进度看板" in body, str(code))
    check("board shows the blocked reason",
          "新版合同模板第 8 条" in body or "考勤机导出" in body, "")
    check("board marks AI-dictated reports", "AI 代笔" in body, "")
    # 口径统一（负责人 10-31）：公司进度中心与部门看板都叫「未汇报」，不再用「从未申报」。
    check("board shows the never-reported metric", "未汇报" in body, "")
    check("board names positions, not people",
          "人事经理" in body and "周岚" not in body, "")
    check("board flags the vacant position", "岗位空缺" in body or "空缺" in body, "")

    code, body, _ = call("/board/body?org=" + str(_org := "x"))
    check("fragment rejects a malformed org id", code in (401, 403, 422), str(code))

    code, body, _ = call("/admin/work-items")
    check("admin work items page renders", code == 200 and "全部工作项" in body, str(code))

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        real_org = conn.execute("SELECT org_unit_id FROM workhub.org_units LIMIT 1").fetchone()["org_unit_id"]
        # Belongs to the same department: a work item for a post outside the room
        # would be refused, and rightly so -- the test has to ask for something legal.
        real_pos = conn.execute("SELECT position_id FROM workhub.positions WHERE org_unit_id = %s LIMIT 1",
                                (real_org,)).fetchone()["position_id"]
    code, body, headers = call("/admin/work-items", method="POST", form={
        "csrf": page_token("/admin/work-items"),
        "position_id": real_pos, "title": "界面创建的测试项", "kind": "routine", "status": "in_progress",
        "blocked_reason": "", "needs_decision": ""})
    check("work item created through the page", code == 303, str(code))
    code, body, _ = call("/admin/work-items")
    check("created item shows in the list", "界面创建的测试项" in body, str(code))
    code, body, headers = call("/admin/work-items", method="POST", form={
        "csrf": page_token("/admin/work-items"),
        "position_id": real_pos, "title": "缺原因的阻塞项", "status": "blocked", "blocked_reason": ""})
    check("page refuses a blocked item without a reason",
          code in (303, 422) and ("blocked" in (hdr(headers, "Location") or "") or code == 422),
          f"{code} {hdr(headers, 'Location')}")

    # --- one layer, one list, and a flow strip that agrees with both ----------
    # A layer is a filter, not a second aggregation, and the flow strip is a row of
    # counters rather than a second copy of the cards. Three things therefore have
    # to reconcile with the API on every combination: the layer badge, the stage
    # counter, and the cards painted in the list. If any two disagree the page is
    # answering a question nobody asked.
    api_board = api_json(f"/v1/board/org-units/{real_org}")
    counts = {layer["id"]: layer["count"] for layer in api_board["layers"]}
    SELECTABLE = ("planned", "in_progress", "blocked")

    code, all_html, _ = call(f"/board/body?org={real_org}&layer=all")
    check("the flow is one strip of counters, not four lanes of cards",
          code == 200 and all_html.count('class="stage ') == 4 and 'class="lane' not in all_html, str(code))
    check("the strip names every stage",
          all(label in all_html for label in ("未开始", "进行中", "阻塞", "已完成")), "")
    check("board fragment offers every layer",
          all(label in all_html for label in ("全部", "阻塞", "待决策", "AI 代笔", "沉默")), "")

    for layer_id in ("all", "blocked", "decision", "agent", "silent"):
        api_layer = api_json(f"/v1/board/org-units/{real_org}?layer={layer_id}")
        _, html, _ = call(f"/board/body?org={real_org}&layer={layer_id}")
        listed = listed_cards(html)
        check(f"layer '{layer_id}' lists exactly its badge count",
              listed == counts[layer_id] and listed == len(api_layer["visible_cards"]),
              f"badge={counts[layer_id]} listed={listed}")
        strip = strip_counts(html)
        check(f"layer '{layer_id}' strip counters add up to the list",
              sum(strip[s] for s in SELECTABLE) == listed, f"{strip} vs {listed}")
        check(f"layer '{layer_id}' strip agrees with the API per stage",
              all(strip[s] == flow["count"] for s in SELECTABLE
                  for flow in api_layer["flow"] if flow["id"] == s), str(strip))
        # 'done' is a counter with no cards behind it, so it is printed but never
        # selectable -- it must still equal the API's count or it is just decor.
        check(f"layer '{layer_id}' prints the terminal count without offering it as a list",
              strip["done"] == next(flow["count"] for flow in api_layer["flow"] if flow["id"] == "done")
              and '<a class="stage done' not in html, str(strip))

    # A stage narrows a layer instead of replacing it: asking for blocked inside
    # the decision layer means decision AND blocked, never one or the other.
    for layer_id in ("all", "decision", "silent"):
        for stage_id in SELECTABLE:
            api_both = api_json(f"/v1/board/org-units/{real_org}?layer={layer_id}&stage={stage_id}")
            _, html, _ = call(f"/board/body?org={real_org}&layer={layer_id}&stage={stage_id}")
            listed = listed_cards(html)
            check(f"layer '{layer_id}' + stage '{stage_id}' lists what the API returns",
                  listed == len(api_both["visible_cards"]),
                  f"listed={listed} api={len(api_both['visible_cards'])}")
            strip = strip_counts(html)
            check(f"layer '{layer_id}' + stage '{stage_id}' keeps the whole layer visible in the strip",
                  sum(strip[s] for s in SELECTABLE) == counts[layer_id], str(strip))

    bogus_code, bogus_html, _ = call(f"/board/body?org={real_org}&layer=bogus")
    check("an unknown layer falls back to all instead of an empty board",
          bogus_code == 200 and listed_cards(bogus_html) == counts["all"], str(bogus_code))
    # 'done' has no cards behind it by design, so asking for it can only fall back
    # to no stage filter -- it must never answer with an empty list and a straight face.
    _, done_html, _ = call(f"/board/body?org={real_org}&stage=done")
    check("'done' cannot be selected as a stage and falls back to the layer",
          listed_cards(done_html) == counts["all"], str(listed_cards(done_html)))
    _, junk_html, _ = call(f"/board/body?org={real_org}&stage=bogus")
    check("an unknown stage falls back to no stage filter",
          listed_cards(junk_html) == counts["all"], str(listed_cards(junk_html)))

    # --- presentation pass: hierarchy, keyboard, and the scoreboard -----------
    # These are the rules that keep three rows of controls from reading as one
    # and keep keyboard users from navigating blind. They fail loudly on purpose:
    # a focus outline or a live region is easy to lose in a template refactor.
    code, page, _ = call("/board")
    check("keyboard focus is visible site-wide", ":focus-visible" in page, "")
    check("department navigation is a segmented control, not another pill row",
          'class="orgnav"' in page and 'aria-label="选择部门"' in page, "")
    check("the board fragment announces itself as a live region",
          'aria-live="polite"' in page, "")
    check("selected controls carry aria-current",
          all_html.count('aria-current="true"') >= 2, str(all_html.count('aria-current="true"')))

    check("the AI scoreboard is a split bar, not a stranded metric card",
          "channel-scorecard" in all_html and "class=\"split\"" in all_html
          and "AI 代笔占比" not in all_html, "")
    check("the implementation note about flow counting is gone from the page",
          "流转条按当前图层计数" not in all_html, "")
    check("card positions render as metadata, not as competing badges",
          'class="pos"' in all_html, "")

    fin = next(flow["count"] for flow in api_board["flow"] if flow["id"] == "done")
    check("a zero terminal count fades instead of glowing green",
          fin != 0 or "terminal zero" in all_html, str(fin))
    any_progress = any(card["latest_report"] and card["latest_report"]["progress_value"] is not None
                       for card in api_board["visible_cards"])
    check("reported progress renders as a bar, not only text",
          not any_progress or '<div class="bar">' in all_html, "")

    code, body, _ = call("/admin/positions")
    check("admin positions page renders", code == 200 and "全部岗位" in body, str(code))

    code, body, _ = call("/admin/audit")
    check("audit page renders", code == 200 and "审计" in body, str(code))

    # --- the specialist: allowed on the board, refused on audit -------------
    opener2 = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                          urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    global_opener = opener
    opener = opener2
    spec_token, _ = issue_session(people["1002"])
    code, body, headers = call(f"/login?token={spec_token}")
    check("specialist can sign in", code == 302 and "workhub_session=" in hdr(headers, "Set-Cookie") or "", str(code))
    code, body, _ = call("/board")
    check("specialist sees the board", code == 200 and "进度看板" in body, str(code))
    # A page they may not read is not an error screen: it is a detour to one they
    # can read, carrying the reason in a short-lived notice cookie.
    code, body, headers = call("/admin/audit")
    landed = hdr(headers, "Location") or ""
    check("a page the specialist may not read sends them somewhere they can go",
          code == 303 and landed.endswith("/progress") and "没有权限" not in body,
          f"{code} {landed}")
    # A well-formed form with the specialist's own token: the refusal has to come
    # from the missing grant, not from a validation error about a missing field.
    code, body, _ = call("/admin/positions", method="POST", form={
        "csrf": session_form_token("workhub:position-form:v1"),
        "position_code": "HACK", "title": "hack", "org_unit_id": real_org})
    check("specialist cannot create a position", code == 403 and "没有权限" in body, str(code))
    code, body, headers = call("/logout", method="POST", form={})
    check("logout redirects to login and clears the cookie",
          code == 302 and hdr(headers, "Location") == "/login", f"{code} {headers.get('Location')}")
    code, body, _ = call("/board")
    check("board after logout redirects to login",
          code == 302 and "/login" in hdr(headers, "Location") or "", str(code))

    failed = [r for r in results if not r[1]]
    assert not failed, f"{len(results) - len(failed)}/{len(results)} checks passed; failed: {failed}"


def test_web_ui_e2e(hr_site) -> None:
    """Run the original standalone script as a pytest test."""

    results.clear()
    _script(hr_site["base"])
