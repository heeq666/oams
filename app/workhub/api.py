from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response, status
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation, ExclusionViolation
from psycopg.types.json import Jsonb
import json

from workhub.db import connection
from workhub.schemas import (AssignmentCreate, GrantCreate, MemberCreate, MemberStatusUpdate, OrgUnitCreate,
                             PositionConfigCreate, PositionCreate, ReportCreate, ReportRetract,
                             WorkItemCreate, WorkItemUpdate)
from workhub.security import current_principal, require_permission, revoke_session

app = FastAPI(title="Company Workhub API", version="0.1.0")


def audit(conn, actor_id: UUID, event_type: str, subject_type: str, subject_id: UUID, reason: str,
          after: dict[str, Any], before: dict[str, Any] | None = None):
    conn.execute(
        """INSERT INTO workhub.workforce_audit_events
           (actor_member_id, event_type, subject_type, subject_id, reason, before_state, after_state)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (actor_id, event_type, subject_type, subject_id, reason,
         Jsonb(before, dumps=lambda obj: json.dumps(obj, default=str)) if before is not None else None,
         Jsonb(after, dumps=lambda obj: json.dumps(obj, default=str))),
    )


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ready", "database": "connected"}


@app.get("/v1/me")
def me(principal: dict = Depends(current_principal)):
    with connection() as conn:
        assignments = conn.execute(
            """SELECT a.assignment_id, a.position_id, p.position_code, p.title,
                      a.assignment_type, a.starts_at, a.ends_at
               FROM workhub.position_assignments a JOIN workhub.positions p USING (position_id)
               WHERE a.member_id = %s AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                 AND (a.ends_at IS NULL OR now() < a.ends_at) ORDER BY a.starts_at""",
            (principal["member_id"],),
        ).fetchall()
    return {"member_id": principal["member_id"], "display_name": principal["display_name"], "assignments": assignments}


@app.get("/v1/org-units")
def list_org_units(_actor: dict = Depends(require_permission("workforce.org_units", "read"))):
    with connection() as conn:
        return conn.execute("SELECT org_unit_id, parent_org_unit_id, org_code, name, unit_type, status FROM workhub.org_units ORDER BY org_code").fetchall()


@app.post("/v1/org-units", status_code=status.HTTP_201_CREATED)
def create_org_unit(payload: OrgUnitCreate, actor: dict = Depends(require_permission("workforce.org_units", "manage"))):
    try:
        with connection() as conn:
            row = conn.execute(
                """INSERT INTO workhub.org_units (org_code, name, parent_org_unit_id, unit_type)
                   VALUES (%s, %s, %s, %s)
                   RETURNING org_unit_id, parent_org_unit_id, org_code, name, unit_type, status""",
                (payload.org_code, payload.name, payload.parent_org_unit_id, payload.unit_type),
            ).fetchone()
            audit(conn, actor["member_id"], "org_unit.created", "org_unit", row["org_unit_id"], "created through management API", row)
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="org_code already exists")
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="parent org unit does not exist")


@app.get("/v1/members")
def list_members(_actor: dict = Depends(require_permission("workforce.members", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT member_id, employee_code, display_name, employment_status, joined_at, departed_at
               FROM workhub.workforce_members ORDER BY display_name"""
        ).fetchall()


@app.post("/v1/members", status_code=status.HTTP_201_CREATED)
def create_member(payload: MemberCreate, actor: dict = Depends(require_permission("workforce.members", "manage"))):
    try:
        with connection() as conn:
            member = conn.execute(
                """INSERT INTO workhub.workforce_members (employee_code, display_name, joined_at)
                   VALUES (%s, %s, %s)
                   RETURNING member_id, employee_code, display_name, employment_status, joined_at""",
                (payload.employee_code, payload.display_name, payload.joined_at),
            ).fetchone()
            audit(conn, actor["member_id"], "member.created", "member", member["member_id"], "created through management API", member)
            return member
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="employee code already exists")


@app.get("/v1/positions")
def list_positions(_actor: dict = Depends(require_permission("workforce.positions", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT p.position_id, p.position_code, p.title, p.org_unit_id, o.org_code, p.status
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               ORDER BY o.org_code, p.position_code"""
        ).fetchall()


@app.post("/v1/positions", status_code=status.HTTP_201_CREATED)
def create_position(payload: PositionCreate, actor: dict = Depends(require_permission("workforce.positions", "manage"))):
    try:
        with connection() as conn:
            return insert_position(conn, payload, actor["member_id"])
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="position_code already exists")
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="org unit does not exist")


@app.get("/v1/assignments")
def list_assignments(member_id: UUID | None = None, position_id: UUID | None = None,
                     _actor: dict = Depends(require_permission("workforce.assignments", "read"))):
    filters = []
    params: list[Any] = []
    if member_id is not None:
        filters.append("a.member_id = %s")
        params.append(member_id)
    if position_id is not None:
        filters.append("a.position_id = %s")
        params.append(position_id)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with connection() as conn:
        return conn.execute(
            f"""SELECT a.assignment_id, a.member_id, m.display_name, a.position_id, p.position_code,
                      p.title, a.assignment_type, a.status, a.starts_at, a.ends_at, a.reason,
                      a.approved_by, a.approved_at
               FROM workhub.position_assignments a
               JOIN workhub.workforce_members m USING (member_id)
               JOIN workhub.positions p USING (position_id)
               {where} ORDER BY a.starts_at DESC""",
            params,
        ).fetchall()


@app.get("/v1/grants")
def list_grants(subject_type: str | None = None, subject_id: UUID | None = None,
               _actor: dict = Depends(require_permission("workforce.grants", "read"))):
    if subject_type not in (None, "member", "position"):
        raise HTTPException(status_code=422, detail="subject_type must be member or position")
    filters = []
    params: list[Any] = []
    if subject_type is not None:
        filters.append("g.subject_type = %s")
        params.append(subject_type)
    if subject_id is not None:
        filters.append("CASE WHEN g.subject_type = 'member' THEN g.member_id ELSE g.position_id END = %s")
        params.append(subject_id)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with connection() as conn:
        return conn.execute(
            f"""SELECT g.grant_id, g.subject_type, g.position_id, g.member_id, g.resource, g.action,
                      g.scope_type, g.scope_org_unit_id, g.scope_definition, g.effect, g.valid_from,
                      g.valid_until, g.revoked_at, g.reason, g.approved_by, g.approved_at
               FROM workhub.permission_grants g {where} ORDER BY g.created_at DESC""",
            params,
        ).fetchall()


@app.post("/v1/assignments", status_code=status.HTTP_201_CREATED)
def create_assignment(payload: AssignmentCreate, actor: dict = Depends(require_permission("workforce.assignments", "manage"))):
    if payload.ends_at and payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be after starts_at")
    state = "active" if payload.starts_at <= datetime.now(timezone.utc) else "scheduled"
    try:
        with connection() as conn:
            eligibility = conn.execute(
                """SELECT m.employment_status, p.status AS position_status
                   FROM workhub.workforce_members m CROSS JOIN workhub.positions p
                   WHERE m.member_id = %s AND p.position_id = %s""",
                (payload.member_id, payload.position_id),
            ).fetchone()
            if not eligibility:
                raise HTTPException(status_code=422, detail="member or position does not exist")
            if eligibility["employment_status"] != "active" or eligibility["position_status"] != "active":
                raise HTTPException(status_code=409, detail="member and position must be active")
            row = conn.execute(
                """INSERT INTO workhub.position_assignments
                   (member_id, position_id, assignment_type, status, starts_at, ends_at, approved_by,
                    approved_at, reason, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
                   RETURNING assignment_id, member_id, position_id, assignment_type, status, starts_at, ends_at""",
                (payload.member_id, payload.position_id, payload.assignment_type, state, payload.starts_at,
                 payload.ends_at, actor["member_id"], payload.reason, actor["member_id"]),
            ).fetchone()
            conn.execute("UPDATE workhub.workforce_members SET token_version = token_version + 1, updated_at = now() WHERE member_id = %s", (payload.member_id,))
            conn.execute("UPDATE workhub.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (payload.member_id,))
            audit(conn, actor["member_id"], "assignment.created", "assignment", row["assignment_id"], payload.reason, row)
            return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="assignment conflicts with existing data")
    except ExclusionViolation:
        raise HTTPException(status_code=409, detail="overlapping assignment for this member and position")
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="member or position does not exist")


@app.post("/v1/assignments/{assignment_id}/end")
def end_assignment(assignment_id: UUID, reason: str, actor: dict = Depends(require_permission("workforce.assignments", "manage"))):
    with connection() as conn:
        prior = conn.execute(
            "SELECT assignment_id, member_id, position_id, status, starts_at, ends_at FROM workhub.position_assignments WHERE assignment_id = %s FOR UPDATE",
            (assignment_id,),
        ).fetchone()
        if not prior or prior["status"] not in ("active", "scheduled"):
            raise HTTPException(status_code=404, detail="active assignment not found")
        future = prior["starts_at"] >= datetime.now(timezone.utc)
        row = conn.execute(
            """UPDATE workhub.position_assignments
               SET status = CASE WHEN %s THEN 'cancelled' ELSE 'ended' END,
                   ends_at = CASE WHEN %s THEN ends_at ELSE LEAST(COALESCE(ends_at, now()), now()) END,
                   ended_at = CASE WHEN %s THEN NULL ELSE now() END, ended_by = %s
               WHERE assignment_id = %s
               RETURNING assignment_id, member_id, position_id, status, starts_at, ends_at""",
            (future, future, future, actor["member_id"], assignment_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="active assignment not found")
        conn.execute("UPDATE workhub.workforce_members SET token_version = token_version + 1, updated_at = now() WHERE member_id = %s", (row["member_id"],))
        conn.execute("UPDATE workhub.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (row["member_id"],))
        audit(conn, actor["member_id"], "assignment.ended", "assignment", row["assignment_id"], reason, row, prior)
        return row


@app.post("/v1/grants", status_code=status.HTTP_201_CREATED)
def create_grant(payload: GrantCreate, actor: dict = Depends(require_permission("workforce.grants", "manage"))):
    if payload.valid_until and payload.valid_until <= payload.valid_from:
        raise HTTPException(status_code=422, detail="valid_until must be after valid_from")
    try:
        with connection() as conn:
            row = conn.execute(
                """INSERT INTO workhub.permission_grants
                   (subject_type, position_id, member_id, resource, action, scope_type, scope_org_unit_id,
                    scope_definition, effect, valid_from, valid_until, reason, approved_by, approved_at, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                   RETURNING grant_id, subject_type, position_id, member_id, resource, action, scope_type,
                             scope_org_unit_id, effect, valid_from, valid_until""",
                (payload.subject_type, payload.position_id, payload.member_id, payload.resource, payload.action,
                 payload.scope_type, payload.scope_org_unit_id, Jsonb(payload.scope_definition), payload.effect,
                 payload.valid_from, payload.valid_until, payload.reason, actor["member_id"], actor["member_id"]),
            ).fetchone()
            subject_type = "position" if payload.position_id else "member"
            subject_id = payload.position_id or payload.member_id
            audit(conn, actor["member_id"], "grant.created", subject_type, subject_id, payload.reason, row)
            return row
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="grant subject or scope does not exist")


@app.get("/v1/positions/{position_id}/config-versions")
def list_position_config_versions(position_id: UUID, _actor: dict = Depends(require_permission("workforce.positions", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT config_version_id, position_id, version_no, status, effective_from, effective_to,
                      based_on_version_id, config, change_reason, created_by, approved_by, approved_at
               FROM workhub.position_config_versions WHERE position_id = %s ORDER BY version_no DESC""",
            (position_id,),
        ).fetchall()


@app.post("/v1/positions/{position_id}/config-versions", status_code=status.HTTP_201_CREATED)
def create_position_config_version(position_id: UUID, payload: PositionConfigCreate,
                                   actor: dict = Depends(require_permission("workforce.positions", "manage"))):
    with connection() as conn:
        exists = conn.execute("SELECT 1 FROM workhub.positions WHERE position_id = %s", (position_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="position not found")
        conn.execute("SELECT position_id FROM workhub.positions WHERE position_id = %s FOR UPDATE", (position_id,))
        version = conn.execute(
            "SELECT COALESCE(max(version_no), 0) + 1 AS next_version FROM workhub.position_config_versions WHERE position_id = %s",
            (position_id,),
        ).fetchone()["next_version"]
        row = conn.execute(
            """INSERT INTO workhub.position_config_versions
               (position_id, version_no, status, effective_from, based_on_version_id, config, change_reason, created_by)
               VALUES (%s, %s, 'draft', %s, %s, %s, %s, %s)
               RETURNING config_version_id, position_id, version_no, status, effective_from, based_on_version_id, config""",
            (position_id, version, payload.effective_from, payload.based_on_version_id,
             Jsonb(payload.config), payload.change_reason, actor["member_id"]),
        ).fetchone()
        audit(conn, actor["member_id"], "position_config.created", "position_config_version", row["config_version_id"], payload.change_reason, row)
        return row


@app.post("/v1/position-config-versions/{version_id}/approve")
def approve_position_config_version(version_id: UUID, actor: dict = Depends(require_permission("workforce.positions", "manage"))):
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM workhub.position_config_versions WHERE config_version_id = %s FOR UPDATE", (version_id,)
        ).fetchone()
        if not row or row["status"] != "draft":
            raise HTTPException(status_code=404, detail="draft config version not found")
        conn.execute("SELECT position_id FROM workhub.positions WHERE position_id = %s FOR UPDATE", (row["position_id"],))
        if row["based_on_version_id"] is not None and not conn.execute(
            "SELECT 1 FROM workhub.position_config_versions WHERE config_version_id = %s AND position_id = %s",
            (row["based_on_version_id"], row["position_id"]),
        ).fetchone():
            raise HTTPException(status_code=422, detail="based_on_version_id must belong to this position")
        next_version = conn.execute(
            """SELECT config_version_id FROM workhub.position_config_versions
               WHERE position_id = %s AND status IN ('active', 'scheduled') AND effective_from > %s
               ORDER BY effective_from LIMIT 1""",
            (row["position_id"], row["effective_from"]),
        ).fetchone()
        if next_version:
            raise HTTPException(status_code=409, detail="configuration cannot be scheduled before an existing active or scheduled version")
        previous = conn.execute(
            """SELECT config_version_id, effective_from FROM workhub.position_config_versions
               WHERE position_id = %s AND status IN ('active', 'scheduled')
                 AND effective_from < %s AND (effective_to IS NULL OR effective_to > %s)
               ORDER BY effective_from DESC LIMIT 1 FOR UPDATE""",
            (row["position_id"], row["effective_from"], row["effective_from"]),
        ).fetchone()
        if previous:
            if row["effective_from"] <= datetime.now(timezone.utc):
                raise HTTPException(status_code=409, detail="a new version for an existing configuration must start in the future")
            conn.execute(
                "UPDATE workhub.position_config_versions SET effective_to = %s WHERE config_version_id = %s",
                (row["effective_from"], previous["config_version_id"]),
            )
        target_status = "active" if row["effective_from"] <= datetime.now(timezone.utc) else "scheduled"
        approved = conn.execute(
            """UPDATE workhub.position_config_versions SET status = %s, approved_by = %s, approved_at = now()
               WHERE config_version_id = %s
               RETURNING config_version_id, position_id, version_no, status, effective_from, effective_to, approved_by, approved_at""",
            (target_status, actor["member_id"], version_id),
        ).fetchone()
        audit(conn, actor["member_id"], "position_config.approved", "position_config_version", version_id, row["change_reason"], approved, row)
        return approved


@app.patch("/v1/members/{member_id}/status")
def update_member_status(member_id: UUID, payload: MemberStatusUpdate,
                         actor: dict = Depends(require_permission("workforce.members", "manage"))):
    with connection() as conn:
        member = conn.execute("SELECT * FROM workhub.workforce_members WHERE member_id = %s FOR UPDATE", (member_id,)).fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="member not found")
        updated = conn.execute(
            """UPDATE workhub.workforce_members SET employment_status = %s,
                      departed_at = CASE WHEN %s = 'departed' THEN CURRENT_DATE
                                         WHEN %s = 'active' THEN NULL ELSE departed_at END,
                      token_version = token_version + 1, updated_at = now()
                WHERE member_id = %s
                RETURNING member_id, display_name, employment_status, departed_at, token_version""",
            (payload.employment_status, payload.employment_status, payload.employment_status, member_id),
        ).fetchone()
        conn.execute("UPDATE workhub.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (member_id,))
        if payload.employment_status in ("departed", "suspended"):
            if payload.employment_status == "departed":
                conn.execute("UPDATE workhub.permission_grants SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (member_id,))
                conn.execute(
                    """UPDATE workhub.position_assignments SET status = 'ended', ends_at = LEAST(COALESCE(ends_at, now()), now()), ended_at = now(), ended_by = %s
                       WHERE member_id = %s AND status = 'active' AND starts_at < now()""",
                    (actor["member_id"], member_id),
                )
                conn.execute(
                    """UPDATE workhub.position_assignments SET status = 'cancelled'
                       WHERE member_id = %s AND status = 'scheduled'""", (member_id,)
                )
        audit(conn, actor["member_id"], "member.status_changed", "member", member_id, payload.reason, updated, member)
        return updated


@app.post("/v1/grants/{grant_id}/revoke")
def revoke_grant(grant_id: UUID, reason: str,
                 actor: dict = Depends(require_permission("workforce.grants", "manage"))):
    with connection() as conn:
        prior = conn.execute("SELECT * FROM workhub.permission_grants WHERE grant_id = %s AND revoked_at IS NULL FOR UPDATE", (grant_id,)).fetchone()
        if not prior:
            raise HTTPException(status_code=404, detail="active grant not found")
        row = conn.execute(
            """UPDATE workhub.permission_grants SET revoked_at = now()
               WHERE grant_id = %s
               RETURNING grant_id, subject_type, position_id, member_id, resource, action, scope_type, revoked_at""",
            (grant_id,),
        ).fetchone()
        audit(conn, actor["member_id"], "grant.revoked", row["subject_type"], row["position_id"] or row["member_id"], reason, row, prior)
        return row


# ---------------------------------------------------------------------------
# Work items and progress reports
#
# SCOPE WARNING: require_permission only understands scope_type='all' today, so
# every endpoint in this section is effectively an all-scope reader until row-level
# range filtering exists. Do not issue a member token on that basis.
#
# One deliberate omission: submitting a report does not change the work item's
# status. The two are kept separate so that appending progress cannot be used to
# rewrite the state of an item behind someone's back; status changes stay explicit
# and auditable through the work item endpoints.
# ---------------------------------------------------------------------------

OPEN_ITEM_STATUSES = ("planned", "in_progress", "blocked")

WORK_ITEM_SELECT = """w.work_item_id, w.org_unit_id, w.position_id, w.title, w.kind, w.status,
                      w.blocked_reason, w.needs_decision, w.created_at, w.updated_at,
                      p.position_code, p.title AS position_title, o.org_code, o.name AS org_name"""


@app.get("/v1/work-items")
def list_work_items(org_unit_id: UUID | None = None, position_id: UUID | None = None, open_only: bool = False,
                    _actor: dict = Depends(require_permission("workforce.work_items", "read"))):
    filters = []
    params: list[Any] = []
    if org_unit_id is not None:
        filters.append("w.org_unit_id = %s")
        params.append(org_unit_id)
    if position_id is not None:
        filters.append("w.position_id = %s")
        params.append(position_id)
    if open_only:
        filters.append("w.status IN ('planned', 'in_progress', 'blocked')")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with connection() as conn:
        return conn.execute(
            f"""SELECT {WORK_ITEM_SELECT}
                  FROM workhub.work_items w
                  JOIN workhub.positions p USING (position_id)
                  JOIN workhub.org_units o ON o.org_unit_id = w.org_unit_id
                  {where}
                 ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                          w.needs_decision DESC, w.updated_at DESC""",
            params,
        ).fetchall()


@app.post("/v1/work-items", status_code=status.HTTP_201_CREATED)
def create_work_item(payload: WorkItemCreate, actor: dict = Depends(require_permission("workforce.work_items", "manage"))):
    try:
        with connection() as conn:
            return insert_work_item(conn, payload, actor["member_id"])
    except CheckViolation:
        raise HTTPException(status_code=422, detail="blocked work items require a non-empty blocked_reason")


@app.patch("/v1/work-items/{work_item_id}")
def update_work_item(work_item_id: UUID, payload: WorkItemUpdate,
                     actor: dict = Depends(require_permission("workforce.work_items", "manage"))):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="no fields supplied")
    try:
        with connection() as conn:
            prior = conn.execute(
                "SELECT * FROM workhub.work_items WHERE work_item_id = %s FOR UPDATE", (work_item_id,)
            ).fetchone()
            if not prior:
                raise HTTPException(status_code=404, detail="work item not found")
            if "status" in updates and updates["status"] == "blocked":
                reason = updates.get("blocked_reason", prior["blocked_reason"])
                if not (reason and reason.strip()):
                    raise HTTPException(status_code=422, detail="blocked work items require a non-empty blocked_reason")
            column_map = {"title": "title", "status": "status", "blocked_reason": "blocked_reason",
                          "needs_decision": "needs_decision"}
            assignments = ", ".join(f"{column_map[field]} = %s" for field in updates)
            params = list(updates.values()) + [work_item_id]
            row = conn.execute(
                f"""UPDATE workhub.work_items SET {assignments}, updated_at = now()
                    WHERE work_item_id = %s
                    RETURNING work_item_id, org_unit_id, position_id, title, kind, status, blocked_reason,
                              needs_decision, created_at, updated_at""",
                params,
            ).fetchone()
            audit(conn, actor["member_id"], "work_item.updated", "work_item", work_item_id,
                  "updated through management API", row, prior)
            return row
    except CheckViolation:
        raise HTTPException(status_code=422, detail="blocked work items require a non-empty blocked_reason")


@app.post("/v1/work-items/{work_item_id}/reports", status_code=status.HTTP_201_CREATED)
def submit_report(work_item_id: UUID, payload: ReportCreate,
                  actor: dict = Depends(require_permission("workforce.reports", "submit"))):
    try:
        with connection() as conn:
            item = conn.execute(
                "SELECT work_item_id, status FROM workhub.work_items WHERE work_item_id = %s FOR UPDATE",
                (work_item_id,),
            ).fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="work item not found")
            if item["status"] not in OPEN_ITEM_STATUSES:
                raise HTTPException(status_code=409, detail="cannot report progress on a closed work item")
            row = conn.execute(
                """INSERT INTO workhub.work_item_reports
                   (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING report_id, work_item_id, reported_by_member_id, submitted_via, progress_value,
                             note, evidence, status, reported_at""",
                (work_item_id, actor["member_id"], payload.submitted_via, payload.progress_value,
                 payload.note, payload.evidence),
            ).fetchone()
            conn.execute("UPDATE workhub.work_items SET updated_at = now() WHERE work_item_id = %s", (work_item_id,))
            audit(conn, actor["member_id"], "work_item_report.created", "work_item_report", row["report_id"],
                  f"progress submitted via {payload.submitted_via}", row)
            return row
    except CheckViolation:
        raise HTTPException(status_code=422, detail="evidence is required and progress_value must be between 0 and 100")


@app.get("/v1/work-items/{work_item_id}/reports")
def list_work_item_reports(work_item_id: UUID,
                           _actor: dict = Depends(require_permission("workforce.reports", "read"))):
    with connection() as conn:
        exists = conn.execute("SELECT 1 FROM workhub.work_items WHERE work_item_id = %s", (work_item_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="work item not found")
        return conn.execute(
            """SELECT r.report_id, r.reported_by_member_id, m.display_name AS reported_by, r.submitted_via,
                      r.progress_value, r.note, r.evidence, r.status, r.retracted_at, r.reported_at
               FROM workhub.work_item_reports r
               JOIN workhub.workforce_members m ON m.member_id = r.reported_by_member_id
               WHERE r.work_item_id = %s ORDER BY r.reported_at DESC""",
            (work_item_id,),
        ).fetchall()


@app.post("/v1/reports/{report_id}/retract")
def retract_report(report_id: UUID, payload: ReportRetract,
                   actor: dict = Depends(require_permission("workforce.reports", "submit"))):
    with connection() as conn:
        prior = conn.execute(
            "SELECT * FROM workhub.work_item_reports WHERE report_id = %s FOR UPDATE", (report_id,)
        ).fetchone()
        if not prior:
            raise HTTPException(status_code=404, detail="report not found")
        if prior["status"] == "retracted":
            raise HTTPException(status_code=409, detail="report already retracted")
        # A member owns what they submitted, including what their own AI typed. The
        # row is marked rather than deleted: retracting must not rewrite history.
        if prior["reported_by_member_id"] != actor["member_id"]:
            raise HTTPException(status_code=403, detail="members can only retract their own reports")
        row = conn.execute(
            """UPDATE workhub.work_item_reports
               SET status = 'retracted', retracted_by_member_id = %s, retracted_at = now()
               WHERE report_id = %s
               RETURNING report_id, work_item_id, submitted_via, status, retracted_at""",
            (actor["member_id"], report_id),
        ).fetchone()
        audit(conn, actor["member_id"], "work_item_report.retracted", "work_item_report", report_id, payload.reason, row, prior)
        return row


# ---------------------------------------------------------------------------
# Board aggregation
#
# The board answers one question for management: what is stuck, what needs a
# decision, and what has nobody touched. It deliberately reports silence too --
# an item with no report, or a position with no holder, is visible rather than
# quietly absent. A board that only shows what people filled in hides exactly the
# cases management needs.
#
# Responsibility is shown as a position, not a person. Work items hang off
# positions so that a departure hands them over automatically, and the board keeps
# that property: holdership changes must not require the board to be redrawn.
# ---------------------------------------------------------------------------

BOARD_QUERY = """SELECT w.work_item_id, w.title, w.kind, w.status, w.blocked_reason, w.needs_decision,
                        w.created_at, w.updated_at,
                        p.position_id, p.position_code, p.title AS position_title,
                        (SELECT count(*) FROM workhub.position_assignments a
                          WHERE a.position_id = p.position_id
                            AND a.status IN ('active', 'scheduled')
                            AND a.starts_at <= now()
                            AND (a.ends_at IS NULL OR now() < a.ends_at)) AS holder_count,
                        s.latest_report_id, s.latest_submitted_via, s.latest_progress_value,
                        s.latest_note, s.latest_evidence, s.latest_reported_at
                   FROM workhub.work_items w
                   JOIN workhub.positions p ON p.position_id = w.position_id
                   LEFT JOIN workhub.work_item_current_state s ON s.work_item_id = w.work_item_id
                  WHERE w.org_unit_id = %s AND w.status IN ('planned', 'in_progress', 'blocked')
                  ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                           w.needs_decision DESC, w.updated_at DESC"""


def _board_card(row: dict[str, Any], now: datetime, stale_after: timedelta) -> dict[str, Any]:
    reported_at = row["latest_reported_at"]
    reported = reported_at is not None
    overdue = row["status"] in ("in_progress", "blocked") and (
        not reported or reported_at < now - stale_after
    )
    return {
        "work_item_id": row["work_item_id"],
        "title": row["title"],
        "kind": row["kind"],
        "status": row["status"],
        "blocked_reason": row["blocked_reason"],
        "needs_decision": row["needs_decision"],
        "updated_at": row["updated_at"],
        "position": {
            "position_id": row["position_id"],
            "position_code": row["position_code"],
            "title": row["position_title"],
            "holder_count": row["holder_count"],
        },
        "latest_report": None if not reported else {
            "report_id": row["latest_report_id"],
            "submitted_via": row["latest_submitted_via"],
            "progress_value": row["latest_progress_value"],
            "note": row["latest_note"],
            "evidence": row["latest_evidence"],
            "reported_at": reported_at,
        },
        "reported": reported,
        "overdue": overdue,
    }


# Board layers are declared once and shared by the JSON endpoint and the page, so
# a layer's badge can never disagree with the count printed beside it. Each layer
# is a plain predicate over the same card list -- a layer is a filter, not a
# second aggregation, which is why the numbers always reconcile.
BOARD_LAYERS: tuple[tuple[str, str, str], ...] = (
    ("all", "全部", "该部门下所有未完成工作项"),
    ("blocked", "阻塞", "状态为阻塞，必须写明卡在哪"),
    ("decision", "待决策", "已被标记需要管理层拍板"),
    ("agent", "AI 代笔", "最新一条进度由 AI 代笔提交"),
    ("silent", "沉默", "从没报过，或超过阈值没更新"),
)

# "blocked" is not really a stage on the way to done -- it is in_progress that got
# stuck. Management still needs it as its own lane, so it sits in the flow as a
# marked branch instead of pretending work marches through it in sequence.
FLOW_STAGES: tuple[tuple[str, str], ...] = (
    ("planned", "未开始"),
    ("in_progress", "进行中"),
    ("blocked", "阻塞"),
    ("done", "已完成"),
)


def layer_matches(layer_id: str, card: dict[str, Any]) -> bool:
    if layer_id == "blocked":
        return card["status"] == "blocked"
    if layer_id == "decision":
        return bool(card["needs_decision"])
    if layer_id == "agent":
        return card["reported"] and card["latest_report"]["submitted_via"] == "agent"
    if layer_id == "silent":
        return not card["reported"] or card["overdue"]
    return True


def build_board(conn, org_unit_id: UUID, stale_days: int = 7) -> dict[str, Any]:
    """The one board aggregation. Both the JSON endpoint and the HTML board render
    from this, so the page can never drift from the API it is supposed to show."""
    stale_days = min(max(stale_days, 1), 365)
    now = datetime.now(timezone.utc)
    stale_after = timedelta(days=stale_days)
    org_unit = conn.execute(
        "SELECT org_unit_id, parent_org_unit_id, org_code, name, unit_type, status FROM workhub.org_units WHERE org_unit_id = %s",
        (org_unit_id,),
    ).fetchone()
    if not org_unit:
        raise HTTPException(status_code=404, detail="org unit not found")
    rows = conn.execute(BOARD_QUERY, (org_unit_id,)).fetchall()
    child_org_units = conn.execute(
        """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
           WHERE parent_org_unit_id = %s AND status = 'active' ORDER BY org_code""",
        (org_unit_id,),
    ).fetchall()

    cards = [_board_card(row, now, stale_after) for row in rows]
    by_position: dict[UUID, dict[str, Any]] = {}
    for card in cards:
        position_id = card["position"]["position_id"]
        bucket = by_position.setdefault(position_id, {
            "position": card["position"],
            "items": [],
        })
        bucket["items"].append(card)

    channel = Counter(card["latest_report"]["submitted_via"] for card in cards if card["reported"])

    layers = [{"id": layer_id, "label": label, "hint": hint,
               "count": sum(1 for card in cards if layer_matches(layer_id, card))}
              for layer_id, label, hint in BOARD_LAYERS]

    # "done" is deliberately a count and not a lane of cards. The board exists to
    # show what is still open, and widening BOARD_QUERY to include finished work
    # would silently redefine every other number on the page. Counting it keeps
    # the exit of the flow visible; the detail lives in the work item list.
    finished = conn.execute(
        """SELECT count(*) AS n FROM workhub.work_items
           WHERE org_unit_id = %s AND status IN ('done', 'cancelled')""",
        (org_unit_id,),
    ).fetchone()["n"]
    flow = []
    for stage_id, label in FLOW_STAGES:
        if stage_id == "done":
            flow.append({"id": stage_id, "label": label, "count": finished, "cards": []})
            continue
        stage_cards = [card for card in cards if card["status"] == stage_id]
        flow.append({"id": stage_id, "label": label, "count": len(stage_cards), "cards": stage_cards})

    return {
        "org_unit": org_unit,
        "generated_at": now,
        "stale_days": stale_days,
        "summary": {
            "open_items": len(cards),
            "blocked": sum(1 for card in cards if card["status"] == "blocked"),
            "needs_decision": sum(1 for card in cards if card["needs_decision"]),
            "never_reported": sum(1 for card in cards if not card["reported"]),
            "overdue": sum(1 for card in cards if card["overdue"]),
            "positions_without_holder": sum(1 for bucket in by_position.values() if bucket["position"]["holder_count"] == 0),
            # The human/agent split of the current state is the scoreboard for v1:
            # if this stays all-human, AI dictation was not adopted in practice.
            "latest_report_by_channel": {"human": channel["human"], "agent": channel["agent"]},
        },
        "cards": cards,
        "layers": layers,
        "flow": flow,
        "sections": {
            "blocked": [card for card in cards if card["status"] == "blocked"],
            "needs_decision": [card for card in cards if card["needs_decision"] and card["status"] != "blocked"],
            "by_position": sorted(by_position.values(), key=lambda bucket: bucket["position"]["position_code"]),
        },
        "child_org_units": child_org_units,
    }


@app.get("/v1/board/org-units/{org_unit_id}")
def board_for_org_unit(org_unit_id: UUID, stale_days: int = 7,
                       _actor: dict = Depends(require_permission("workforce.work_items", "read"))):
    with connection() as conn:
        return build_board(conn, org_unit_id, stale_days)


def insert_work_item(conn, payload: WorkItemCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared by the JSON endpoint and the admin page; one insert, one audit."""
    position = conn.execute(
        "SELECT p.position_id, p.org_unit_id, p.status FROM workhub.positions p WHERE p.position_id = %s",
        (payload.position_id,),
    ).fetchone()
    if not position:
        raise HTTPException(status_code=404, detail="position not found")
    if position["status"] != "active":
        raise HTTPException(status_code=409, detail="work items can only be attached to an active position")
    row = conn.execute(
        """INSERT INTO workhub.work_items
           (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING work_item_id, org_unit_id, position_id, title, kind, status, blocked_reason,
                     needs_decision, created_at, updated_at""",
        (position["org_unit_id"], payload.position_id, payload.title, payload.kind, payload.status,
         payload.blocked_reason, payload.needs_decision, actor_member_id),
    ).fetchone()
    audit(conn, actor_member_id, "work_item.created", "work_item", row["work_item_id"],
          "created through management API", row)
    return row


def insert_position(conn, payload: PositionCreate, actor_member_id: Any) -> dict[str, Any]:
    position = conn.execute(
        """INSERT INTO workhub.positions (position_code, title, org_unit_id)
           VALUES (%s, %s, %s)
           RETURNING position_id, position_code, title, org_unit_id, status""",
        (payload.position_code, payload.title, payload.org_unit_id),
    ).fetchone()
    audit(conn, actor_member_id, "position.created", "position", position["position_id"],
          "created through management API", position)
    return position


@app.get("/v1/audit-events")
def list_audit_events(limit: int = 100, _actor: dict = Depends(require_permission("workforce.audit", "read"))):
    limit = min(max(limit, 1), 500)
    with connection() as conn:
        return conn.execute(
            """SELECT event_id, actor_member_id, event_type, subject_type, subject_id, occurred_at, reason,
                      before_state, after_state, metadata
               FROM workhub.workforce_audit_events ORDER BY occurred_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()


@app.post("/v1/sessions/current/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_current_session(principal: dict = Depends(current_principal)):
    with connection() as conn:
        revoke_session(conn, principal["session_id"], principal["member_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Web UI
#
# The pages are mounted on the same app as the JSON API so there is one process to
# run and one session table to reason about. The only thing the UI layer adds is a
# cookie carrier: the token in that cookie is an ordinary session, resolved by
# security.principal_for_token and checked by security.has_permission -- the same
# functions the bearer scheme uses. Nothing here grants anything by itself.
# ---------------------------------------------------------------------------
from workhub.web import router as web_router  # noqa: E402
from workhub.security import principal_from_request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(web_router)


@app.middleware("http")
async def attach_browser_principal(request, call_next):
    # Cheap when absent: API callers send no cookie, so no lookup happens at all.
    request.state.member = principal_from_request(request)
    return await call_next(request)
