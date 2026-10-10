"""Load one department's real people, positions and work items through the API.

Why this goes through HTTP rather than psql: the point of the seeding pass is that
it exercises the same authorisation, validation and audit path that normal use
will. Loading straight into the database would bypass all of it and leave a
department nobody ever created through the system.

Everything here is idempotent. Running it twice creates nothing the second time,
which matters because real data arrives in fits: names get corrected, work items
get added later.

    # 1. print exactly what would be written, change nothing
    WORKHUB_MIGRATOR_URL=... WORKHUB_ADMIN_MEMBER_ID=... \\
        python scripts/load_department_data.py data/example-department.json

    # 2. write it
    ... python scripts/load_department_data.py data/example-department.json --apply

The admin token is minted for this run only; the API service itself is started as
workhub_runtime, exactly as it runs in production.
"""
import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("WORKHUB_LOADER_PORT", "8126"))
BASE = f"http://127.0.0.1:{PORT}"
REASON = "initial department load"

planned: list[str] = []


def log(message: str) -> None:
    planned.append(message)
    print(message, flush=True)


class Api:
    def __init__(self, token: str):
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw


def wait_for_service(proc) -> bool:
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            if proc.poll() is not None:
                return False
            time.sleep(0.25)
    return False


def find(api: Api, path: str, key: str, value: str):
    status, rows = api.call("GET", path)
    if status != 200:
        raise SystemExit(f"cannot list {path}: {status} {rows}")
    return next((row for row in rows if row.get(key) == value), None)


def load(api: Api, spec: dict, apply_changes: bool) -> None:
    unit = spec["org_unit"]
    existing_unit = find(api, "/v1/org-units", "org_code", unit["code"])
    if existing_unit:
        log(f"[exists]  org unit   {unit['code']}  {existing_unit['org_unit_id']}")
    else:
        log(f"[create]  org unit   {unit['code']}  {unit['name']}")
        if apply_changes:
            status, created = api.call("POST", "/v1/org-units", {
                "org_code": unit["code"], "name": unit["name"],
                "unit_type": unit.get("unit_type", "department"),
            })
            if status != 201:
                raise SystemExit(f"failed to create org unit: {status} {created}")

    unit_row = find(api, "/v1/org-units", "org_code", unit["code"])
    # In a dry run the unit was never created, so downstream rows can only be
    # reported as planned. A null id is enough for that; nothing is written.
    unit_id = unit_row["org_unit_id"] if unit_row else None

    position_ids: dict[str, str] = {}
    for position in spec["positions"]:
        existing = find(api, "/v1/positions", "position_code", position["code"])
        if existing:
            log(f"[exists]  position   {position['code']}  {existing['title']}")
            position_ids[position["code"]] = existing["position_id"]
        else:
            log(f"[create]  position   {position['code']}  {position['title']}")
            if apply_changes:
                status, created = api.call("POST", "/v1/positions", {
                    "position_code": position["code"], "title": position["title"], "org_unit_id": unit_id})
                if status != 201:
                    raise SystemExit(f"failed to create position {position['code']}: {status} {created}")
                position_ids[position["code"]] = created["position_id"]
            else:
                position_ids[position["code"]] = None

    starts_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    now_repr = starts_at.isoformat()

    for member in spec["members"]:
        existing = find(api, "/v1/members", "employee_code", member["employee_code"])
        if existing:
            log(f"[exists]  member     {member['employee_code']}  {existing['display_name']}")
            member_id = existing["member_id"]
            # A member enters the system pending and needs explicit activation, so a
            # seeded person is not automatically usable. Never auto-reactivate anyone
            # who was deliberately suspended or departed.
            if existing["employment_status"] == "pending":
                log(f"[activate] member   {member['employee_code']}  pending -> active")
                if apply_changes:
                    status, _ = api.call("PATCH", f"/v1/members/{member_id}/status",
                                         {"employment_status": "active", "reason": REASON})
                    if status != 200:
                        raise SystemExit(f"failed to activate {member['employee_code']}: {status}")
            elif existing["employment_status"] != "active":
                log(f"[skip]    member     {member['employee_code']} is {existing['employment_status']}, left alone")
        else:
            log(f"[create]  member     {member['employee_code']}  {member['name']}")
            if apply_changes:
                status, created = api.call("POST", "/v1/members", {
                    "employee_code": member["employee_code"], "display_name": member["name"],
                    "joined_at": member.get("joined_at")})
                if status != 201:
                    raise SystemExit(f"failed to create member {member['employee_code']}: {status} {created}")
                member_id = created["member_id"]
                status, _ = api.call("PATCH", f"/v1/members/{member_id}/status",
                                     {"employment_status": "active", "reason": REASON})
                if status != 200:
                    raise SystemExit(f"failed to activate {member['employee_code']}: {status}")
            else:
                member_id = None

        position_id = position_ids.get(member["position_code"])
        if member_id is None or position_id is None:
            log(f"[skip]    assignment {member['employee_code']} -> {member['position_code']} (dry run)")
            continue
        status, assignments = api.call("GET", f"/v1/assignments?member_id={member_id}")
        held = [row for row in assignments
                if row["position_id"] == position_id and row["status"] in ("active", "scheduled")]
        if held:
            log(f"[exists]  assignment {member['employee_code']} -> {member['position_code']}")
            continue
        log(f"[create]  assignment {member['employee_code']} -> {member['position_code']}")
        if apply_changes:
            status, created = api.call("POST", "/v1/assignments", {
                "member_id": member_id, "position_id": position_id,
                "assignment_type": member.get("assignment_type", "primary"),
                "starts_at": now_repr, "reason": REASON})
            if status != 201:
                raise SystemExit(f"failed to assign {member['employee_code']}: {status} {created}")

    for item in spec.get("work_items", []):
        position_id = position_ids.get(item["position_code"])
        status, items = api.call("GET", f"/v1/work-items?position_id={position_id}") if position_id else (0, [])
        existing = next((row for row in items if row["title"] == item["title"]), None) if position_id else None
        if existing:
            log(f"[exists]  work item  {item['title']}")
            continue
        log("[create]  work item  " + item["title"] + f"  ({item['position_code']}, {item.get('status', 'planned')})")
        if apply_changes:
            status, created = api.call("POST", "/v1/work-items", {
                "position_id": position_id, "title": item["title"],
                "kind": item.get("kind", "routine"), "status": item.get("status", "planned"),
                "blocked_reason": item.get("blocked_reason"), "needs_decision": item.get("needs_decision", False)})
            if status != 201:
                raise SystemExit(f"failed to create work item {item['title']}: {status} {created}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load one department through the Workhub API")
    parser.add_argument("spec", help="path to the department JSON file")
    parser.add_argument("--apply", action="store_true", help="write changes; without it this is a dry run")
    args = parser.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    migrator_url = os.environ.get("WORKHUB_MIGRATOR_URL")
    admin_id = os.environ.get("WORKHUB_ADMIN_MEMBER_ID")
    if not migrator_url or not admin_id:
        raise SystemExit("WORKHUB_MIGRATOR_URL and WORKHUB_ADMIN_MEMBER_ID are required to mint a session token")

    sys.path.insert(0, str(ROOT / "app"))
    # Sessions are minted as the migration operator; the service itself must keep
    # whatever runtime connection the environment already points at.
    service_url = os.environ.get("WORKHUB_DATABASE_URL")
    os.environ["WORKHUB_DATABASE_URL"] = migrator_url
    from workhub.security import issue_session

    token, expires_at = issue_session(admin_id)
    print(f"session minted for this run, expires {expires_at.isoformat()}", flush=True)
    print("DRY RUN -- nothing will be written" if not args.apply else "APPLYING CHANGES", flush=True)

    child_env = dict(os.environ)
    if service_url is None:
        child_env.pop("WORKHUB_DATABASE_URL", None)
    else:
        child_env["WORKHUB_DATABASE_URL"] = service_url
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "workhub.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for_service(proc):
            raise SystemExit("API service did not start")
        load(Api(token), spec, args.apply)
        # Revoke before the service goes away: the loader's session should not
        # outlive the run.
        Api(token).call("POST", "/v1/sessions/current/revoke")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    created = len([line for line in planned if line.startswith("[create]")])
    reused = len([line for line in planned if line.startswith("[exists]")])
    print(f"\n{created} new, {reused} already present, session revoked")


if __name__ == "__main__":
    main()
