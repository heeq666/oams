"""Load a department's people, positions, grants and work items through the API.

Why this goes through HTTP rather than psql: the point of the seeding pass is that
it exercises the same authorisation, validation and audit path that normal use
will. Loading straight into the database would bypass all of it and leave a
department nobody ever created through the system.

Everything here is idempotent. Running it twice creates nothing the second time,
which matters because data arrives in fits: names get corrected, work items get
added later, a second department follows the first.

    # 1. print exactly what would be written, change nothing
    OAMS_DATABASE_URL=... OAMS_ADMIN_MEMBER_ID=... \\
        python scripts/load_department_data.py data/example-department.json

    # 2. write it
    ... python scripts/load_department_data.py data/example-department.json --apply

The admin token is minted for this run only; the API service itself is started as
workhub_runtime, exactly as it runs in production.

Spec format:

    {
      "org_units":  [{"code", "name", "unit_type", "parent_code"}],
      "positions":  [{"code", "title", "org_unit_code"}],
      "members":    [{"name", "employee_code", "position_code", "assignment_type"}],
      "grants":     [{"subject_code" | "member_code", "resource", "action",
                      "scope_type", "reason"}],
      "work_items": [{"position_code", "title", "kind", "status",
                      "blocked_reason", "needs_decision"}],
      "reports":    [{"employee_code", "position_code", "work_item_title",
                      "submitted_via", "progress_value", "note", "evidence"}]
    }

Org units are created in the order given, so a parent must precede its children;
units that already exist (the bootstrapped company, say) are reused rather than
duplicated. Reports are submitted under a short-lived session minted for that
member, because a report belongs to whoever signed it -- an administrator cannot
submit progress on somebody else's behalf.
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
PORT = int(os.environ.get("OAMS_LOADER_PORT", "8126"))
BASE = f"http://127.0.0.1:{PORT}"
REASON = "initial department load"
DEFAULT_GRANT_REASON = "minimum permission needed to do the job"

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


def load_org_units(api: Api, spec: dict, apply_changes: bool) -> dict[str, str]:
    # The older single-object shape is still accepted so existing spec files keep
    # working; everything downstream only needs the code -> id map.
    units = spec.get("org_units") or [spec["org_unit"]]
    fallback_code = units[0]["code"]
    unit_ids: dict[str, str | None] = {}
    for unit in units:
        existing = find(api, "/v1/org-units", "org_code", unit["code"])
        if existing:
            log(f"[exists]  org unit   {unit['code']}  {existing['name']}")
            unit_ids[unit["code"]] = existing["org_unit_id"]
            continue
        parent_id = unit_ids.get(unit["parent_code"]) if unit.get("parent_code") else None
        log(f"[create]  org unit   {unit['code']}  {unit['name']}"
            + (f"  under {unit['parent_code']}" if unit.get("parent_code") else ""))
        if apply_changes:
            status, created = api.call("POST", "/v1/org-units", {
                "org_code": unit["code"], "name": unit["name"],
                "parent_org_unit_id": parent_id,
                "unit_type": unit.get("unit_type", "department"),
            })
            if status != 201:
                raise SystemExit(f"failed to create org unit {unit['code']}: {status} {created}")
            unit_ids[unit["code"]] = created["org_unit_id"]
        else:
            unit_ids[unit["code"]] = None
    return {code: unit_id for code, unit_id in unit_ids.items()}, fallback_code


def load_positions(api: Api, spec: dict, unit_ids: dict[str, str | None], fallback_code: str,
                   apply_changes: bool) -> dict[str, str | None]:
    position_ids: dict[str, str | None] = {}
    for position in spec["positions"]:
        existing = find(api, "/v1/positions", "position_code", position["code"])
        if existing:
            log(f"[exists]  position   {position['code']}  {existing['title']}")
            position_ids[position["code"]] = existing["position_id"]
            continue
        unit_code = position.get("org_unit_code", fallback_code)
        log(f"[create]  position   {position['code']}  {position['title']}  ({unit_code})")
        if apply_changes:
            status, created = api.call("POST", "/v1/positions", {
                "position_code": position["code"], "title": position["title"],
                "org_unit_id": unit_ids.get(unit_code)})
            if status != 201:
                raise SystemExit(f"failed to create position {position['code']}: {status} {created}")
            position_ids[position["code"]] = created["position_id"]
        else:
            position_ids[position["code"]] = None
    return position_ids


def load_members(api: Api, spec: dict, position_ids: dict[str, str | None], apply_changes: bool) -> dict[str, str | None]:
    starts_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    now_repr = starts_at.isoformat()
    member_ids: dict[str, str | None] = {}
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
        member_ids[member["employee_code"]] = member_id

        position_id = position_ids.get(member["position_code"])
        if member_id is None or position_id is None:
            log(f"[skip]    assignment {member['employee_code']} -> {member['position_code']} (nothing to attach to yet)")
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
    return member_ids


def load_grants(api: Api, spec: dict, position_ids: dict[str, str | None],
                member_ids: dict[str, str | None], apply_changes: bool) -> None:
    # Every grant here is written with scope_type='all' unless a spec says
    # otherwise, and that is a debt rather than a design: require_permission only
    # understands 'all' today, so anything narrower would silently grant nothing.
    # Row-level range filtering has to land before a second department joins.
    for grant in spec.get("grants", []):
        if grant.get("member_code"):
            subject_type, subject_id, shown = "member", member_ids.get(grant["member_code"]), grant["member_code"]
        else:
            subject_type, subject_id, shown = "position", position_ids.get(grant["subject_code"]), grant["subject_code"]
        if subject_id is None:
            log(f"[skip]    grant      {shown} {grant['resource']}/{grant['action']} (subject missing)")
            continue
        status, existing_grants = api.call("GET", f"/v1/grants?subject_type={subject_type}&subject_id={subject_id}")
        duplicate = any(row["resource"] == grant["resource"] and row["action"] == grant["action"]
                        and row["revoked_at"] is None and row["scope_type"] == grant.get("scope_type", "all")
                        for row in existing_grants)
        if duplicate:
            log(f"[exists]  grant      {shown} {grant['resource']}/{grant['action']}")
            continue
        label = f"{shown} {grant['resource']}/{grant['action']} scope={grant.get('scope_type', 'all')}"
        log(f"[create]  grant      {label}")
        if apply_changes:
            status, created = api.call("POST", "/v1/grants", {
                "subject_type": subject_type,
                "member_id": subject_id if subject_type == "member" else None,
                "position_id": subject_id if subject_type == "position" else None,
                "resource": grant["resource"], "action": grant["action"],
                "scope_type": grant.get("scope_type", "all"),
                "valid_from": dt.datetime.now(dt.timezone.utc).isoformat(),
                "reason": grant.get("reason", DEFAULT_GRANT_REASON)})
            if status != 201:
                raise SystemExit(f"failed to grant {label}: {status} {created}")


def load_work_items(api, spec: dict, position_ids: dict[str, str | None], apply_changes: bool) -> None:
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


def load_reports(issue_session, api: Api, spec: dict, member_ids: dict[str, str | None],
                 position_ids: dict[str, str | None], apply_changes: bool) -> None:
    for report in spec.get("reports", []):
        member_id = member_ids.get(report["employee_code"])
        position_id = position_ids.get(report["position_code"])
        if member_id is None or position_id is None:
            log(f"[skip]    report     {report['work_item_title']} (member or position missing)")
            continue
        status, items = api.call("GET", f"/v1/work-items?position_id={position_id}")
        item = next((row for row in items if row["title"] == report["work_item_title"]), None)
        if item is None:
            log(f"[skip]    report     {report['work_item_title']} (work item missing)")
            continue
        label = f"{report['employee_code']} {report['submitted_via']} {report.get('progress_value')}% {report['work_item_title']}"
        status, history = api.call("GET", f"/v1/work-items/{item['work_item_id']}/reports")
        duplicate = any(row["note"] == report.get("note") and row["status"] == "active" for row in history)
        if duplicate:
            log(f"[exists]  report     {label}")
            continue
        log(f"[create]  report     {label}")
        if apply_changes:
            # Signed by the member themselves through a session minted for this one
            # call, then revoked: a report must name the person who stands behind it.
            token, _ = issue_session(member_id)
            try:
                members_api = Api(token)
                status, created = members_api.call("POST", f"/v1/work-items/{item['work_item_id']}/reports", {
                    "submitted_via": report["submitted_via"], "progress_value": report.get("progress_value"),
                    "note": report.get("note"), "evidence": report["evidence"]})
                if status != 201:
                    raise SystemExit(f"failed to submit report on {report['work_item_title']}: {status} {created}")
            finally:
                Api(token).call("POST", "/v1/sessions/current/revoke")


def load(api: Api, issue_session, spec: dict, apply_changes: bool) -> None:
    unit_ids, fallback_code = load_org_units(api, spec, apply_changes)
    position_ids = load_positions(api, spec, unit_ids, fallback_code, apply_changes)
    member_ids = load_members(api, spec, position_ids, apply_changes)
    load_grants(api, spec, position_ids, member_ids, apply_changes)
    load_work_items(api, spec, position_ids, apply_changes)
    load_reports(issue_session, api, spec, member_ids, position_ids, apply_changes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load one department through the Workhub API")
    parser.add_argument("spec", help="path to the department JSON file")
    parser.add_argument("--apply", action="store_true", help="write changes; without it this is a dry run")
    args = parser.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    admin_id = os.environ.get("OAMS_ADMIN_MEMBER_ID")
    if not admin_id:
        raise SystemExit("OAMS_ADMIN_MEMBER_ID is required to mint a session token")

    sys.path.insert(0, str(ROOT / "app"))
    from oams.security import issue_session

    token, expires_at = issue_session(admin_id)
    print(f"session minted for this run, expires {expires_at.isoformat()}", flush=True)
    print("DRY RUN -- nothing will be written" if not args.apply else "APPLYING CHANGES", flush=True)

    child_env = dict(os.environ)
    if service_url is None:
        child_env.pop("OAMS_DATABASE_URL", None)
    else:
        child_env["OAMS_DATABASE_URL"] = service_url
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "workhub.api:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for_service(proc):
            raise SystemExit("API service did not start")
        load(Api(token), issue_session, spec, args.apply)
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
