"""End-to-end check of the server-rendered UI: login cookie, board, admin pages.

It needs a throwaway database that has already been seeded, because it signs in as
the seeded administrator and as the seeded specialist:

    docker run -d --name workhub-web-test \
        -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=workhub_test \
        -p 55433:5432 postgres:16-alpine
    export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@localhost:55433/workhub_test
    python -m workhub.cli migrate
    ADMIN=$(python -m workhub.cli bootstrap-admin --name example-user --employee-code 9999 \
        --org-code example-co --org-name example-co | python -c "import json,sys;print(json.load(sys.stdin)['member_id'])")
    WORKHUB_MIGRATOR_URL=$WORKHUB_DATABASE_URL WORKHUB_ADMIN_MEMBER_ID=$ADMIN \
        python scripts/load_department_data.py data/seed-hr-department.json --apply
    python tests/test_web_ui_e2e.py

Exit code 0 means every assertion passed. The checks that matter most are the
negative ones: an unauthenticated board redirects, and the specialist -- who has
no audit grant -- is refused the audit page rather than shown a thinner version
of it.
"""
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])
PORT = 8128
BASE = f"http://127.0.0.1:{PORT}"
results = []


def check(label, passed, detail=""):
    results.append((label, passed, detail))
    print(("PASS  " if passed else "FAIL  ") + label + (f"  [{detail}]" if detail else ""), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def lane_counts(html):
    """(count in the lane header, cards actually painted in that lane)."""
    out = {}
    for stage, inner in re.findall(r'<section class="lane (\w+)">([\s\S]*?)</section>', html):
        header = re.search(r'<span class="cnt">(\d+)</span>', inner)
        out[stage] = (int(header.group(1)) if header else None, inner.count('class="mini '))
    return out


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


sys.path.insert(0, ROOT + "/app")
os.environ.setdefault("WORKHUB_DATABASE_URL", os.environ["WORKHUB_DATABASE_URL"])
from workhub.security import issue_session  # noqa: E402
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import urllib.parse  # noqa: E402

with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
    people = {row["employee_code"]: row["member_id"]
              for row in conn.execute("SELECT employee_code, member_id FROM workhub.workforce_members").fetchall()}

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "workhub.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ready = False
    for _ in range(120):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                ready = True
                break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.25)
    check("service ready", ready)

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
    check("board shows the silence metric", "从未申报" in body, "")
    check("board names positions, not people",
          "人事经理" in body and "周岚" not in body, "")
    check("board flags the vacant position", "岗位空缺" in body or "空缺" in body, "")

    code, body, _ = call("/board/body?org=" + str(_org := "x"))
    check("fragment rejects a malformed org id", code in (401, 403, 422), str(code))

    code, body, _ = call("/admin/work-items")
    check("admin work items page renders", code == 200 and "全部工作项" in body, str(code))

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        real_org = conn.execute("SELECT org_unit_id FROM workhub.org_units LIMIT 1").fetchone()["org_unit_id"]
        real_pos = conn.execute("SELECT position_id FROM workhub.positions LIMIT 1").fetchone()["position_id"]
    code, body, headers = call("/admin/work-items", method="POST", form={
        "position_id": real_pos, "title": "界面创建的测试项", "kind": "routine", "status": "in_progress",
        "blocked_reason": "", "needs_decision": ""})
    check("work item created through the page", code == 303, str(code))
    code, body, _ = call("/admin/work-items")
    check("created item shows in the list", "界面创建的测试项" in body, str(code))
    code, body, headers = call("/admin/work-items", method="POST", form={
        "position_id": real_pos, "title": "缺原因的阻塞项", "status": "blocked", "blocked_reason": ""})
    check("page refuses a blocked item without a reason",
          code in (303, 422) and ("blocked" in (hdr(headers, "Location") or "") or code == 422),
          f"{code} {hdr(headers, 'Location')}")

    # --- layers and flow lanes ------------------------------------------------
    # A layer is a filter, not a second aggregation. The count on the badge and
    # the cards actually painted in the lanes come from the same predicate, so if
    # they ever disagree the page is lying to itself.
    api_board = api_json(f"/v1/board/org-units/{real_org}")
    counts = {layer["id"]: layer["count"] for layer in api_board["layers"]}

    code, all_html, _ = call(f"/board/body?org={real_org}&layer=all")
    check("board fragment renders every flow lane",
          code == 200 and all(label in all_html for label in ("未开始", "进行中", "阻塞", "已完成")), str(code))
    check("board fragment offers every layer",
          all(label in all_html for label in ("全部", "阻塞", "待决策", "AI 代笔", "沉默")), "")

    for layer_id in ("blocked", "decision", "agent", "silent"):
        _, html, _ = call(f"/board/body?org={real_org}&layer={layer_id}")
        painted = html.count('class="mini ')
        listed = html.count('<li class="')
        check(f"layer '{layer_id}' paints exactly its badge count",
              painted == counts[layer_id] and listed == counts[layer_id],
              f"badge={counts[layer_id]} lanes={painted} detail={listed}")
        # A header claiming 4 cards above a lane holding 1 is the page disagreeing
        # with itself, which is exactly what made the first cut of this confusing.
        lanes = lane_counts(html)
        check(f"layer '{layer_id}' lane headers match the cards painted",
              all(header == painted_in for header, painted_in in lanes.values()), str(lanes))

    bogus_code, bogus_html, _ = call(f"/board/body?org={real_org}&layer=bogus")
    check("an unknown layer falls back to all instead of an empty board",
          bogus_code == 200 and bogus_html.count('class="mini ') == counts["all"], str(bogus_code))

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
    code, body, _ = call("/admin/audit")
    check("specialist is refused the audit page", code == 403 and "没有权限" in body, str(code))
    code, body, _ = call("/admin/positions", method="POST", form={
        "position_code": "HACK", "title": "hack", "org_unit_id": real_org})
    check("specialist cannot create a position", code == 403 and "没有权限" in body, str(code))
    opener = global_opener

    code, body, headers = call("/logout", method="POST", form={})
    check("logout redirects to login and clears the cookie",
          code == 302 and hdr(headers, "Location") == "/login", f"{code} {headers.get('Location')}")
    code, body, _ = call("/board")
    check("board after logout redirects to login",
          code == 302 and "/login" in hdr(headers, "Location") or "", str(code))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
finally:
    proc.terminate()
    proc.wait(timeout=10)
