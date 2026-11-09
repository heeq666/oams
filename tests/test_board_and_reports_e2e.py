"""End-to-end check of work items, progress reports and the board aggregation.

It needs a throwaway database, because it creates organisations, positions, members
and an administrator. Never point it at a real deployment.

    docker run -d --name oams-board-test \
        -e POSTGRES_PASSWORD=oams_test_pw -e POSTGRES_DB=oams_test \
        -p 55433:5432 postgres:16-alpine
    export OAMS_DATABASE_URL=postgresql://postgres:oams_test_pw@localhost:55433/oams_test
    python -m oams.cli migrate
    python tests/test_board_and_reports_e2e.py

Exit code 0 means every assertion passed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

sys.path.insert(0, "app")

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from oams.security import issue_session  # noqa: E402

# Never write to ``os.environ`` here.  pytest imports every test module during
# collection, so a module-level ``setdefault`` used to plant this script's docker
# URL in the environment before any test ran -- and every db-marked test in the
# rest of the suite then probed *that* URL and skipped, even on a machine with a
# perfectly good database.  ``_script()`` rebinds DATABASE_URL from the fixture;
# importing the module must stay side-effect free.
DATABASE_URL = os.environ.get("OAMS_DATABASE_URL", "")

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
        "SELECT org_unit_id FROM oams.org_units WHERE unit_type = 'company' LIMIT 1"
    ).fetchone()["org_unit_id"]
    dept = cur.execute(
        "INSERT INTO oams.org_units (org_code, name, parent_org_unit_id, unit_type) VALUES ('HR', '人事部', %s, 'department') RETURNING org_unit_id",
        (company,),
    ).fetchone()["org_unit_id"]

    positions = {}
    for code, title in [("HRMGR", "人事经理"), ("HRSPC", "人事专员")]:
        positions[code] = cur.execute(
            "INSERT INTO oams.positions (position_code, title, org_unit_id) VALUES (%s, %s, %s) RETURNING position_id",
            (code, title, dept),
        ).fetchone()["position_id"]

    members = {}
    for code, name, position_code in [("E1001", "李经理", "HRMGR"), ("E1002", "王专员", "HRSPC")]:
        member_id = cur.execute(
            "INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES (%s, %s, 'active') RETURNING member_id",
            (code, name),
        ).fetchone()["member_id"]
        cur.execute(
            """INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, reason)
               VALUES (%s, %s, 'primary', 'active', now() - interval '30 days', 'seeding for board test')""",
            (member_id, positions[position_code]),
        )
        members[code] = member_id

    items = {}
    # Blocked, awaiting a decision: the first thing management should see.
    items["blocked"] = cur.execute(
        """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision)
           VALUES (%s, %s, '校园招聘宣讲会场地未确认', 'project', 'blocked', '行政部未回复场地审批', true) RETURNING work_item_id""",
        (dept, positions["HRMGR"]),
    ).fetchone()["work_item_id"]
    items["in_progress"] = cur.execute(
        """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '员工入职材料电子化', 'project', 'in_progress') RETURNING work_item_id""",
        (dept, positions["HRSPC"]),
    ).fetchone()["work_item_id"]
    items["silent"] = cur.execute(
        """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '月度考勤核对', 'routine', 'in_progress') RETURNING work_item_id""",
        (dept, positions["HRSPC"]),
    ).fetchone()["work_item_id"]
    items["done"] = cur.execute(
        """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
           VALUES (%s, %s, '上季度绩效归档', 'routine', 'done') RETURNING work_item_id""",
        (dept, positions["HRMGR"]),
    ).fetchone()["work_item_id"]

    # Explicit timestamps, an hour apart. Two reports inserted in the same
    # statement share one now(), and work_item_current_state breaks that tie on
    # report_id -- a random uuid. That is deterministic but arbitrary, so an
    # assertion about which report wins must not depend on it.
    cur.execute(
        """INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence, reported_at)
           VALUES (%s, %s, 'human', 40, '已联系三家外包', '邮件 thread 2026-10-08', now() - interval '1 hour')""",
        (items["in_progress"], members["E1002"]),
    )
    cur.execute(
        """INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence, reported_at)
           VALUES (%s, %s, 'agent', 60, 'AI 汇总本周邮件后填报', 'AI 依据：mailbox export 2026-10-10', now())""",
        (items["in_progress"], members["E1002"]),
    )
    return {"company": company, "dept": dept, "positions": positions, "members": members, "items": items}


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script(base: str, url: str) -> None:
    """Run every assertion against a freshly migrated database served at *base*."""

    global BASE, DATABASE_URL
    BASE = base
    DATABASE_URL = url
    boot = subprocess.run(
        [sys.executable, "-m", "oams.cli", "bootstrap-admin", "--name", "Admin", "--employee-code", "9999",
         "--org-code", "acme", "--org-name", "ACME"],
        capture_output=True, text=True, cwd=".", env={**os.environ, "PYTHONPATH": "app"},
    )
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        admin_row = conn.execute(
            """SELECT DISTINCT COALESCE(g.member_id, m.member_id) AS member_id
                 FROM oams.permission_grants g
                 LEFT JOIN oams.position_assignments a ON a.position_id = g.position_id
                 LEFT JOIN oams.workforce_members m ON m.member_id = a.member_id
                WHERE g.resource = 'workforce.grants' AND g.action = 'manage'
                  AND g.scope_type = 'all' AND g.revoked_at IS NULL
                  AND COALESCE(g.member_id, m.member_id) IS NOT NULL"""
        ).fetchone()
    check("bootstrap-admin produced an administrator", admin_row is not None,
          (boot.stdout or boot.stderr).strip().replace("\n", " | ")[:160])
    admin_id = str(admin_row["member_id"])

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        fixtures = seed(conn)

    # ``oams db init`` 是唯一建表入口（``app/oams/schema.sql``，全部 IF NOT EXISTS）。
    # 增量迁移 0001-0012 已随 f9fae3d 删除，「模拟老部署再跑 0003 回填」那个场景随方案
    # 一起消失；改成钉住它现在的契约：**重复执行成功，且已经写进去的数据一行不动**。
    #
    # 断言不能是「0 == 0」那种空跑：上一步 bootstrap-admin 已经造成员与基线授权，
    # 先断言它们真的存在（否则这条测试等于什么都没验），再连跑两遍 db init 比三样。
    def _snapshot() -> dict:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            return {
                "members": conn.execute(
                    "SELECT member_id FROM oams.workforce_members ORDER BY member_id").fetchall(),
                "grants": conn.execute(
                    "SELECT grant_id FROM oams.permission_grants ORDER BY grant_id").fetchall(),
                "tables": conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'oams' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name").fetchall(),
            }

    before = _snapshot()
    check("db init has data worth protecting", bool(before["members"]) and bool(before["grants"]),
          f"成员 {len(before['members'])} 行 / 授权 {len(before['grants'])} 行 / 表 {len(before['tables'])} 张")

    reinit = [
        subprocess.run(
            [sys.executable, "-m", "oams.cli", "db", "init"],
            capture_output=True, text=True, cwd=".", env={**os.environ, "PYTHONPATH": "app"},
        )
        for _ in range(2)
    ]
    after = _snapshot()
    check("db init is idempotent", all(r.returncode == 0 for r in reinit),
          " | ".join((r.stdout or r.stderr).strip().replace("\n", " ")[:80] for r in reinit))
    check("db init leaves members, grants and tables untouched", before == after,
          f"成员 {len(before['members'])}→{len(after['members'])} / "
          f"授权 {len(before['grants'])}→{len(after['grants'])} / "
          f"表 {len(before['tables'])}→{len(after['tables'])}")

    token, _ = issue_session(admin_id)

    check("service became ready", True, base)

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
        check("a session cannot claim to be an agent", False, f"{code} {str(second)[:400]}")
        failed = [item for item in results if not item[1]]
        assert not failed, f"{len(failed)}/{len(results)} checks failed: {failed}"
    # 这一条原来断言「能自报 agent」。那正是洞：谁都会说自己填的，客户端能传就等于
    # 这张成绩单没有。现在由服务端按「是不是接入方换来的票」判，人会话里怎么传都是 human。
    check("a session cannot claim to be an agent",
          code == 201 and second["submitted_via"] == "human", f"{code} {second.get('submitted_via')}")

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

    failed = [item for item in results if not item[1]]
    for label, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label}" + (f"  <- {detail}" if detail else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    assert not failed, f"{len(failed)}/{len(results)} checks failed: {failed}"


def test_board_and_reports_e2e(migrated_site) -> None:
    results.clear()
    _script(migrated_site["base"], migrated_site["url"])
