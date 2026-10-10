"""End-to-end check of work items, progress reports and the board aggregation.

It needs a throwaway database, because it creates organisations, positions, members
and an administrator. Never point it at a real deployment.

    docker run -d --name workhub-board-test \
        -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=workhub_test \
        -p 55433:5432 postgres:16-alpine
    export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@localhost:55433/workhub_test
    python -m workhub.cli migrate
    python tests/test_board_and_reports_e2e.py

Exit code 0 means every assertion passed.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "app")
os.environ.setdefault("WORKHUB_DATABASE_URL", "postgresql://postgres:change-me@localhost:55433/workhub_test")

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from workhub.security import issue_session  # noqa: E402

DATABASE_URL = os.environ["WORKHUB_DATABASE_URL"]
PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    results.append((label, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {label}" + (f"  <- {detail}" if detail else ""), flush=True)


def api(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body, default=str).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    # The sandbox exports HTTP proxy variables; localhost calls must not use them.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            payload = resp.read().decode()
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload


def seed(conn) -> dict:
    cur = conn.cursor()
    company = cur.execute(
        "SELECT org_unit_id FROM workhub.org_units WHERE unit_type = 'company' LIMIT 1"
    ).fetchone()["org_unit_id"]
    dept = cur.execute(
        "INSERT INTO workhub.org_units (org_code, name, parent_org_unit_id, unit_type) VALUES ('HR', '人事部', %s, 'department') RETURNING org_unit_id",
        (company,),
    ).fetchone()["org_unit_id"]

    positions = {}
    for code, title in [("HRMGR", "人事经理"), ("HRSPC", "人事专员")]:
        positions[code] = cur.execute(
            "INSERT INTO workhub.positions (position_code, title, org_unit_id) VALUES (%s, %s, %s) RETURNING position_id",
            (code, title, dept),
        ).fetchone()["position_id"]

    members = {}
    for code, name, position_code in [("E1001", "李经理", "HRMGR"), ("E1002", "王专员", "HRSPC")]:
        member_id = cur.execute(
            "INSERT INTO workhub.workforce_members (employee_code, display_name, employment_status) VALUES (%s, %s, 'active') RETURNING member_id",
            (code, name),
        ).fetchone()["member_id"]
        cur.execute(
            """INSERT INTO workhub.position_assignments (member_id, position_id, assignment_type, status, starts_at, reason)
               VALUES (%s, %s, 'primary', 'active', now() - interval '30 days', 'seeding for board test')""",
            (member_id, positions[position_code]),
        )
        members[code] = member_id

    items = {}
    # Blocked, awaiting a decision: the first thing management should see.
    items["blocked"] = cur.execute(
        """INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision)
           VALUES (%s, %s, '校园招聘宣讲会场地未确认', 'project', 'blocked', '行政部未回复场地审批', true) RETURNING work_item_id""",
        (dept, positions["HRMGR"]),
    ).fetchone()["work_item_id"]
    items["in_progress"] = cur.execute(
        """INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '员工入职材料电子化', 'project', 'in_progress') RETURNING work_item_id""",
        (dept, positions["HRSPC"]),
    ).fetchone()["work_item_id"]
    items["silent"] = cur.execute(
        """INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '月度考勤核对', 'routine', 'in_progress') RETURNING work_item_id""",
        (dept, positions["HRSPC"]),
    ).fetchone()["work_item_id"]
    items["done"] = cur.execute(
        """INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '上季度绩效归档', 'routine', 'done') RETURNING work_item_id""",
        (dept, positions["HRMGR"]),
    ).fetchone()["work_item_id"]

    cur.execute(
        """INSERT INTO workhub.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
           VALUES (%s, %s, 'human', 40, '已联系三家外包', '邮件 thread 2026-10-08')""",
        (items["in_progress"], members["E1002"]),
    )
    cur.execute(
        """INSERT INTO workhub.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
           VALUES (%s, %s, 'agent', 60, 'AI 汇总本周邮件后填报', 'AI 依据：mailbox export 2026-10-10')""",
        (items["in_progress"], members["E1002"]),
    )
    return {"company": company, "dept": dept, "positions": positions, "members": members, "items": items}


def main() -> None:
    boot = subprocess.run(
        [sys.executable, "-m", "workhub.cli", "bootstrap-admin", "--name", "example-user", "--employee-code", "9999",
         "--org-code", "example-co", "--org-name", "example-co"],
        capture_output=True, text=True, cwd=".", env={**os.environ, "PYTHONPATH": "app"},
    )
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        admin_row = conn.execute(
            """SELECT DISTINCT COALESCE(g.member_id, m.member_id) AS member_id
                 FROM workhub.permission_grants g
                 LEFT JOIN workhub.position_assignments a ON a.position_id = g.position_id
                 LEFT JOIN workhub.workforce_members m ON m.member_id = a.member_id
                WHERE g.resource = 'workforce.grants' AND g.action = 'manage'
                  AND g.scope_type = 'all' AND g.revoked_at IS NULL
                  AND COALESCE(g.member_id, m.member_id) IS NOT NULL"""
        ).fetchone()
    check("bootstrap-admin produced an administrator", admin_row is not None,
          (boot.stdout or boot.stderr).strip().replace("\n", " | ")[:160])
    admin_id = str(admin_row["member_id"])

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        fixtures = seed(conn)
        # Simulate a deployment created before migration 0003 existed.
        legacy_position = conn.execute(
            "INSERT INTO workhub.positions (position_code, title, org_unit_id) VALUES ('LEGACY', 'Legacy admin', %s) RETURNING position_id",
            (fixtures["company"],),
        ).fetchone()["position_id"]
        conn.execute(
            """INSERT INTO workhub.permission_grants
               (subject_type, position_id, resource, action, scope_type, effect, valid_from, reason)
               VALUES ('position', %s, 'workforce.grants', 'manage', 'all', 'allow', now(), 'pre-0003 administrator')""",
            (legacy_position,),
        )

    # Apply migration 0003, which has to pick the missing action up on its own.
    applied = subprocess.run(
        [sys.executable, "-m", "workhub.cli", "migrate"],
        capture_output=True, text=True, cwd=".", env={**os.environ, "PYTHONPATH": "app"},
    )
    check("migrate is idempotent", "Already applied" in applied.stdout, applied.stdout.strip().replace("\n", " | ")[:120])

    # Apply 0003 the way an existing deployment meets it: admin grants already in
    # place, then the migration runs once and has to derive the missing action.
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute(open("app/workhub/migrations/0003_report_submission_grants.sql").read())
        duplicates = conn.execute(
            """SELECT count(*) AS n FROM workhub.permission_grants
               WHERE resource = 'workforce.reports' AND action = 'submit' AND revoked_at IS NULL
                 AND position_id = %s""", (legacy_position,)).fetchone()["n"]
        conn.execute(open("app/workhub/migrations/0003_report_submission_grants.sql").read())
        twice = conn.execute(
            """SELECT count(*) AS n FROM workhub.permission_grants
               WHERE resource = 'workforce.reports' AND action = 'submit' AND revoked_at IS NULL
                 AND position_id = %s""", (legacy_position,)).fetchone()["n"]
    check("0003 backfill adds submit without duplicating", duplicates == 1 and twice == 1, f"after1={duplicates} after2={twice}")

    token, _ = issue_session(admin_id)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "workhub.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": "app"},
    )
    try:
        ready = False
        for _ in range(80):
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    ready = True
                    break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        check("service became ready", ready, "")

        # --- writes -------------------------------------------------------
        code, body = api("POST", "/v1/work-items", {
            "position_id": fixtures["positions"]["HRMGR"], "title": "  ", "kind": "project"}, token)
        check("blank title rejected", code == 422, str(code))

        code, body = api("POST", "/v1/work-items", {
            "position_id": fixtures["positions"]["HRMGR"], "title": "无原因阻塞", "status": "blocked"}, token)
        check("blocked without reason rejected", code == 422, str(code))

        code, body = api("POST", "/v1/work-items", {
            "position_id": str(uuid.uuid4()), "title": "孤儿工作项"}, token)
        check("unknown position rejected", code == 404, str(code))

        code, created = api("POST", "/v1/work-items", {
            "position_id": fixtures["positions"]["HRMGR"], "title": "招聘渠道复盘",
            "kind": "project", "status": "blocked", "blocked_reason": "缺渠道投放数据",
            "needs_decision": True}, token)
        check("create work item", code == 201 and created.get("org_unit_id") == str(fixtures["dept"]),
              f"{code} derived={created.get('org_unit_id')} expected={fixtures['dept']}")

        new_item = created["work_item_id"] if code == 201 else None

        code, body = api("POST", f"/v1/work-items/{fixtures['items']['done']}/reports",
                         {"submitted_via": "human", "evidence": "x"}, token)
        check("report on closed item rejected", code == 409, str(code))

        code, body = api("POST", f"/v1/work-items/{new_item}/reports",
                         {"submitted_via": "human", "evidence": "   "}, token)
        check("blank evidence rejected", code == 422, str(code))

        code, body = api("POST", f"/v1/work-items/{new_item}/reports",
                         {"submitted_via": "agent", "progress_value": 120, "evidence": "x"}, token)
        check("progress 120 rejected", code == 422, str(code))

        code, first = api("POST", f"/v1/work-items/{new_item}/reports",
                          {"submitted_via": "human", "progress_value": 20, "note": "起步", "evidence": "会议记录"}, token)
        check("submit report", code == 201 and isinstance(first, dict) and first.get("submitted_via") == "human", f"{code} {first if not isinstance(first, dict) else ''}")

        code, second = api("POST", f"/v1/work-items/{new_item}/reports",
                           {"submitted_via": "agent", "progress_value": 50, "note": "AI 汇总", "evidence": "邮箱导出"}, token)
        if not isinstance(second, dict):
            check("submit agent report", False, f"{code} {str(second)[:400]}")
            raise SystemExit(1)
        check("submit agent report", code == 201 and second["submitted_via"] == "agent", str(code))

        code, board_before = api("GET", f"/v1/board/org-units/{fixtures['dept']}", token=token)
        check("board responds", code == 200, str(code))

        # --- retraction ---------------------------------------------------
        code, retracted = api("POST", f"/v1/reports/{second['report_id']}/retract", {"reason": "AI 填错了"}, token)
        check("retract own report", code == 200 and retracted["status"] == "retracted", str(code))

        code, body = api("POST", f"/v1/reports/{second['report_id']}/retract", {"reason": "again"}, token)
        check("double retraction rejected", code == 409, str(code))

        # --- updates ------------------------------------------------------
        # An item that was never blocked has no reason to inherit, so the API has to
        # reject this instead of letting the database raise.
        code, body = api("PATCH", f"/v1/work-items/{fixtures['items']['silent']}", {"status": "blocked"}, token)
        check("patch to blocked without reason rejected", code == 422, str(code))

        code, body = api("PATCH", f"/v1/work-items/{fixtures['items']['silent']}",
                         {"status": "blocked", "blocked_reason": "缺考勤数据"}, token)
        check("patch to blocked with reason allowed", code == 200, str(code))

        code, body = api("PATCH", f"/v1/work-items/{fixtures['items']['silent']}",
                         {"status": "in_progress", "blocked_reason": None}, token)
        check("patch back to in_progress", code == 200, str(code))

        # --- board --------------------------------------------------------
        code, board = api("GET", f"/v1/board/org-units/{fixtures['dept']}", token=token)
        summary = board["summary"]
        check("board hides closed items",
              all(card["work_item_id"] != fixtures["items"]["done"]
                  for group in board["sections"]["by_position"] for card in group["items"]), "")
        check("board counts open items", summary["open_items"] == 4, json.dumps(summary["open_items"]))
        check("blocked surfaced first", len(board["sections"]["blocked"]) == 2, str(len(board["sections"]["blocked"])))
        check("needs_decision section", len(board["sections"]["needs_decision"]) == 0,
              str(len(board["sections"]["needs_decision"])))
        # The blocked item and the routine item both exist but nobody filed anything.
        check("silence counted", summary["never_reported"] == 2, str(summary["never_reported"]))
        check("human/agent split", summary["latest_report_by_channel"] == {"human": 1, "agent": 1},
              json.dumps(summary["latest_report_by_channel"]))
        retracted_fallback = next(
            card for group in board["sections"]["by_position"] for card in group["items"]
            if card["work_item_id"] == new_item)
        check("retraction falls back to previous report",
              retracted_fallback["latest_report"]["report_id"] == first["report_id"],
              str(retracted_fallback["latest_report"]["report_id"]))
        check("responsibility shown as position, not member",
              set(retracted_fallback["position"]) == {"position_id", "position_code", "title", "holder_count"},
              str(list(retracted_fallback["position"])))
        check("no_member_names_on_board",
              "李经理" not in json.dumps(board, ensure_ascii=False) and "王专员" not in json.dumps(board, ensure_ascii=False),
              "")
        check("overdue detected", summary["overdue"] >= 1, str(summary["overdue"]))

        # --- auth ---------------------------------------------------------
        code, body = api("GET", f"/v1/board/org-units/{fixtures['dept']}")
        check("board requires session", code == 401, str(code))

        code, body = api("GET", f"/v1/work-items/{new_item}/reports", token=token)
        check("report history visible", code == 200 and len(body) == 2 and body[0]["status"] == "retracted",
              str(code) + " " + str(len(body) if code == 200 else body))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [item for item in results if not item[1]]
    for label, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label}" + (f"  <- {detail}" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
