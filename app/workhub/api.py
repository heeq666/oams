from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response, status
from psycopg.errors import ForeignKeyViolation, UniqueViolation, ExclusionViolation
from psycopg.types.json import Jsonb
import json

from workhub.db import connection
from workhub.schemas import (AssignmentCreate, GrantCreate, MemberCreate, MemberStatusUpdate, OrgUnitCreate,
                             PositionConfigCreate, PositionCreate)
from workhub.security import current_principal, require_permission

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
            position = conn.execute(
                """INSERT INTO workhub.positions (position_code, title, org_unit_id)
                   VALUES (%s, %s, %s)
                   RETURNING position_id, position_code, title, org_unit_id, status""",
                (payload.position_code, payload.title, payload.org_unit_id),
            ).fetchone()
            audit(conn, actor["member_id"], "position.created", "position", position["position_id"], "created through management API", position)
            return position
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
        conn.execute("UPDATE workhub.workforce_sessions SET revoked_at = now() WHERE session_id = %s", (principal["session_id"],))
        conn.execute(
            """INSERT INTO workhub.workforce_audit_events
               (actor_member_id, event_type, subject_type, subject_id, reason)
               VALUES (%s, 'session.revoked', 'session', %s, 'member ended the current session')""",
            (principal["member_id"], principal["session_id"]),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
