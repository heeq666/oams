from collections import Counter
from urllib.parse import parse_qs
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Response, status
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation, ExclusionViolation
from psycopg.types.json import Jsonb
import json

from oams.db import connection
from oams.schemas import (AssignmentCreate, GrantCreate, MemberCreate, MemberStatusUpdate, OrgUnitCreate,
                             PositionConfigCreate, PositionCreate, ReportCreate, ReportRetract,
                             WorkItemCreate, WorkItemUpdate)
from oams.security import (active_org_unit_ids, current_principal, has_company_scope,
                              has_permission, require_org_unit_access, require_permission, revoke_session)

app = FastAPI(title="Company OAMS API", version="0.1.0")


def audit(conn, actor_id: UUID, event_type: str, subject_type: str, subject_id: UUID, reason: str,
          after: dict[str, Any], before: dict[str, Any] | None = None):
    conn.execute(
        """INSERT INTO oams.workforce_audit_events
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
               FROM oams.position_assignments a JOIN oams.positions p USING (position_id)
               WHERE a.member_id = %s AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                 AND (a.ends_at IS NULL OR now() < a.ends_at) ORDER BY a.starts_at""",
            (principal["member_id"],),
        ).fetchall()
    return {"member_id": principal["member_id"], "display_name": principal["display_name"], "assignments": assignments}


@app.get("/v1/org-units")
def list_org_units(_actor: dict = Depends(require_permission("workforce.org_units", "read"))):
    with connection() as conn:
        return conn.execute("SELECT org_unit_id, parent_org_unit_id, org_code, name, unit_type, status FROM oams.org_units ORDER BY org_code").fetchall()


def insert_org_unit(conn, payload: OrgUnitCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared department creation for the JSON API and the setup page."""
    try:
        row = conn.execute(
            """INSERT INTO oams.org_units (org_code, name, parent_org_unit_id, unit_type)
               VALUES (%s, %s, %s, %s)
               RETURNING org_unit_id, parent_org_unit_id, org_code, name, unit_type, status""",
            (payload.org_code, payload.name, payload.parent_org_unit_id, payload.unit_type),
        ).fetchone()
        audit(conn, actor_member_id, "org_unit.created", "org_unit", row["org_unit_id"], "created through management API", row)
        return row
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="org_code already exists")
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="parent org unit does not exist")


# 409 的两种真相各有自己的机器可读标记（放在响应头里，不进 detail）。
#
# 为什么需要它：两个冲突都走 409，但说的是两件完全不同的事——「这个员工编号已经
# 有人用了」和「这个岗位已经坐着别人了」。调用方（/setup 建成员那张表单）如果把
# 两者都翻成前者，第二次把人往同一岗位放的人会被一句假话堵住，回头去改一个根本
# 没错的编号。detail 是给人看的英文句子，随时可以改措辞，不能当判据；psycopg 抛
# 出来的约束名在 api 这一层已经变成了 HTTPException，所以在这里把它转成一个稳定
# 的标记，调用方按标记分，不按字符串分。
CONFLICT_EMPLOYEE_CODE = "employee_code_taken"
CONFLICT_POSITION_OCCUPIED = "position_occupied"
CONFLICT_HEADER = "X-OAMS-Conflict"


def _conflict(code: str, detail: str) -> HTTPException:
    """409 with a stable machine-readable reason beside the human sentence."""
    return HTTPException(status_code=409, detail=detail, headers={CONFLICT_HEADER: code})


def insert_assignment(conn, payload: AssignmentCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared position assignment for the JSON API and the setup page."""
    if payload.ends_at and payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be after starts_at")
    state = "active" if payload.starts_at <= datetime.now(timezone.utc) else "scheduled"
    try:
        eligibility = conn.execute(
            """SELECT m.employment_status, p.status AS position_status
                 FROM oams.workforce_members m CROSS JOIN oams.positions p
                WHERE m.member_id = %s AND p.position_id = %s""",
            (payload.member_id, payload.position_id),
        ).fetchone()
        if not eligibility:
            raise HTTPException(status_code=422, detail="member or position does not exist")
        if eligibility["employment_status"] != "active" or eligibility["position_status"] != "active":
            raise HTTPException(status_code=409, detail="member and position must be active")
        row = conn.execute(
            """INSERT INTO oams.position_assignments
                  (member_id, position_id, assignment_type, status, starts_at, ends_at, approved_by,
                   approved_at, reason, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
               RETURNING assignment_id, member_id, position_id, assignment_type, status, starts_at, ends_at""",
            (payload.member_id, payload.position_id, payload.assignment_type, state, payload.starts_at,
             payload.ends_at, actor_member_id, payload.reason, actor_member_id),
        ).fetchone()
        conn.execute("UPDATE oams.workforce_members SET token_version = token_version + 1, updated_at = now() WHERE member_id = %s", (payload.member_id,))
        conn.execute("UPDATE oams.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (payload.member_id,))
        audit(conn, actor_member_id, "assignment.created", "assignment", row["assignment_id"], payload.reason, row)
        return row
    except UniqueViolation:
        raise _conflict(CONFLICT_EMPLOYEE_CODE, "assignment conflicts with existing data")
    except ExclusionViolation:
        # 走到这里几乎总是 schema.sql:753 的
        # position_assignments_single_primary_per_position（一个岗位同时只能坐一个人）；
        # 同一个人在同一岗位的两段重叠由 :729 的 position_assignments_no_overlap 挡，
        # 两条约束说的都是「这段安排和已有安排撞了」，而 /setup 这张表单只会遇到前者
        # ——它每次都新造成员，成员不可能已在这个岗位上。
        raise _conflict(CONFLICT_POSITION_OCCUPIED, "overlapping assignment for this member and position")
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="member or position does not exist")


def insert_grant(conn, payload: GrantCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared permission grant for the JSON API and the setup page."""
    if payload.valid_until and payload.valid_until <= payload.valid_from:
        raise HTTPException(status_code=422, detail="valid_until must be after valid_from")
    try:
        row = conn.execute(
            """INSERT INTO oams.permission_grants
                  (subject_type, position_id, member_id, resource, action, scope_type, scope_org_unit_id,
                   scope_definition, effect, valid_from, valid_until, reason, approved_by, approved_at, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
               RETURNING grant_id, subject_type, position_id, member_id, resource, action, scope_type,
                         scope_org_unit_id, effect, valid_from, valid_until""",
            (payload.subject_type, payload.position_id, payload.member_id, payload.resource, payload.action,
             payload.scope_type, payload.scope_org_unit_id, Jsonb(payload.scope_definition), payload.effect,
             payload.valid_from, payload.valid_until, payload.reason, actor_member_id, actor_member_id),
        ).fetchone()
        subject_type = "position" if payload.position_id else "member"
        subject_id = payload.position_id or payload.member_id
        audit(conn, actor_member_id, "grant.created", subject_type, subject_id, payload.reason, row)
        return row
    except ForeignKeyViolation:
        raise HTTPException(status_code=422, detail="grant subject or scope does not exist")


@app.post("/v1/org-units", status_code=status.HTTP_201_CREATED)
def create_org_unit(payload: OrgUnitCreate, actor: dict = Depends(require_permission("workforce.org_units", "manage"))):
    with connection() as conn:
        return insert_org_unit(conn, payload, actor["member_id"])


@app.get("/v1/members")
def list_members(_actor: dict = Depends(require_permission("workforce.members", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT member_id, employee_code, display_name, employment_status, joined_at, departed_at
               FROM oams.workforce_members ORDER BY display_name"""
        ).fetchall()


def insert_member(conn, payload: MemberCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared by the JSON API and the setup page: one insert, one audit, active."""
    try:
        member = conn.execute(
            """INSERT INTO oams.workforce_members (employee_code, display_name, joined_at, employment_status)
               VALUES (%s, %s, %s, 'active')
               RETURNING member_id, employee_code, display_name, employment_status, joined_at""",
            (payload.employee_code, payload.display_name, payload.joined_at),
        ).fetchone()
        audit(conn, actor_member_id, "member.created", "member", member["member_id"], "created through management API", member)
        return member
    except UniqueViolation:
        raise _conflict(CONFLICT_EMPLOYEE_CODE, "employee code already exists")


def activate_member(conn, member_id: Any, actor_member_id: Any, reason: str) -> dict[str, Any]:
    """Restore a pending or suspended account to active and invalidate old sessions."""
    member = conn.execute("SELECT * FROM oams.workforce_members WHERE member_id = %s FOR UPDATE", (member_id,)).fetchone()
    if not member:
        raise HTTPException(status_code=404, detail="member not found")
    updated = conn.execute(
        """UPDATE oams.workforce_members SET employment_status = 'active', departed_at = NULL,
                  token_version = token_version + 1, updated_at = now()
           WHERE member_id = %s
           RETURNING member_id, display_name, employment_status, departed_at, token_version""",
        (member_id,),
    ).fetchone()
    conn.execute("UPDATE oams.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (member_id,))
    audit(conn, actor_member_id, "member.status_changed", "member", member_id, reason, updated, member)
    return updated


@app.post("/v1/members", status_code=status.HTTP_201_CREATED)
def create_member(payload: MemberCreate, actor: dict = Depends(require_permission("workforce.members", "manage"))):
    with connection() as conn:
        return insert_member(conn, payload, actor["member_id"])


@app.get("/v1/positions")
def list_positions(_actor: dict = Depends(require_permission("workforce.positions", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT p.position_id, p.position_code, p.title, p.org_unit_id, o.org_code, p.status
               FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
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
               FROM oams.position_assignments a
               JOIN oams.workforce_members m USING (member_id)
               JOIN oams.positions p USING (position_id)
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
               FROM oams.permission_grants g {where} ORDER BY g.created_at DESC""",
            params,
        ).fetchall()


@app.post("/v1/assignments", status_code=status.HTTP_201_CREATED)
def create_assignment(payload: AssignmentCreate, actor: dict = Depends(require_permission("workforce.assignments", "manage"))):
    with connection() as conn:
        return insert_assignment(conn, payload, actor["member_id"])


@app.post("/v1/assignments/{assignment_id}/end")
def end_assignment(assignment_id: UUID, reason: str, actor: dict = Depends(require_permission("workforce.assignments", "manage"))):
    with connection() as conn:
        prior = conn.execute(
            "SELECT assignment_id, member_id, position_id, status, starts_at, ends_at FROM oams.position_assignments WHERE assignment_id = %s FOR UPDATE",
            (assignment_id,),
        ).fetchone()
        if not prior or prior["status"] not in ("active", "scheduled"):
            raise HTTPException(status_code=404, detail="active assignment not found")
        future = prior["starts_at"] >= datetime.now(timezone.utc)
        row = conn.execute(
            """UPDATE oams.position_assignments
               SET status = CASE WHEN %s THEN 'cancelled' ELSE 'ended' END,
                   ends_at = CASE WHEN %s THEN ends_at ELSE LEAST(COALESCE(ends_at, now()), now()) END,
                   ended_at = CASE WHEN %s THEN NULL ELSE now() END, ended_by = %s
               WHERE assignment_id = %s
               RETURNING assignment_id, member_id, position_id, status, starts_at, ends_at""",
            (future, future, future, actor["member_id"], assignment_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="active assignment not found")
        conn.execute("UPDATE oams.workforce_members SET token_version = token_version + 1, updated_at = now() WHERE member_id = %s", (row["member_id"],))
        conn.execute("UPDATE oams.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (row["member_id"],))
        audit(conn, actor["member_id"], "assignment.ended", "assignment", row["assignment_id"], reason, row, prior)
        return row


@app.post("/v1/grants", status_code=status.HTTP_201_CREATED)
def create_grant(payload: GrantCreate, actor: dict = Depends(require_permission("workforce.grants", "manage"))):
    with connection() as conn:
        return insert_grant(conn, payload, actor["member_id"])


@app.get("/v1/positions/{position_id}/config-versions")
def list_position_config_versions(position_id: UUID, _actor: dict = Depends(require_permission("workforce.positions", "read"))):
    with connection() as conn:
        return conn.execute(
            """SELECT config_version_id, position_id, version_no, status, effective_from, effective_to,
                      based_on_version_id, config, change_reason, created_by, approved_by, approved_at
               FROM oams.position_config_versions WHERE position_id = %s ORDER BY version_no DESC""",
            (position_id,),
        ).fetchall()


@app.post("/v1/positions/{position_id}/config-versions", status_code=status.HTTP_201_CREATED)
def create_position_config_version(position_id: UUID, payload: PositionConfigCreate,
                                   actor: dict = Depends(require_permission("workforce.positions", "manage"))):
    with connection() as conn:
        exists = conn.execute("SELECT 1 FROM oams.positions WHERE position_id = %s", (position_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="position not found")
        conn.execute("SELECT position_id FROM oams.positions WHERE position_id = %s FOR UPDATE", (position_id,))
        version = conn.execute(
            "SELECT COALESCE(max(version_no), 0) + 1 AS next_version FROM oams.position_config_versions WHERE position_id = %s",
            (position_id,),
        ).fetchone()["next_version"]
        row = conn.execute(
            """INSERT INTO oams.position_config_versions
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
            "SELECT * FROM oams.position_config_versions WHERE config_version_id = %s FOR UPDATE", (version_id,)
        ).fetchone()
        if not row or row["status"] != "draft":
            raise HTTPException(status_code=404, detail="draft config version not found")
        conn.execute("SELECT position_id FROM oams.positions WHERE position_id = %s FOR UPDATE", (row["position_id"],))
        if row["based_on_version_id"] is not None and not conn.execute(
            "SELECT 1 FROM oams.position_config_versions WHERE config_version_id = %s AND position_id = %s",
            (row["based_on_version_id"], row["position_id"]),
        ).fetchone():
            raise HTTPException(status_code=422, detail="based_on_version_id must belong to this position")
        next_version = conn.execute(
            """SELECT config_version_id FROM oams.position_config_versions
               WHERE position_id = %s AND status IN ('active', 'scheduled') AND effective_from > %s
               ORDER BY effective_from LIMIT 1""",
            (row["position_id"], row["effective_from"]),
        ).fetchone()
        if next_version:
            raise HTTPException(status_code=409, detail="configuration cannot be scheduled before an existing active or scheduled version")
        previous = conn.execute(
            """SELECT config_version_id, effective_from FROM oams.position_config_versions
               WHERE position_id = %s AND status IN ('active', 'scheduled')
                 AND effective_from < %s AND (effective_to IS NULL OR effective_to > %s)
               ORDER BY effective_from DESC LIMIT 1 FOR UPDATE""",
            (row["position_id"], row["effective_from"], row["effective_from"]),
        ).fetchone()
        if previous:
            if row["effective_from"] <= datetime.now(timezone.utc):
                raise HTTPException(status_code=409, detail="a new version for an existing configuration must start in the future")
            conn.execute(
                "UPDATE oams.position_config_versions SET effective_to = %s WHERE config_version_id = %s",
                (row["effective_from"], previous["config_version_id"]),
            )
        target_status = "active" if row["effective_from"] <= datetime.now(timezone.utc) else "scheduled"
        approved = conn.execute(
            """UPDATE oams.position_config_versions SET status = %s, approved_by = %s, approved_at = now()
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
        member = conn.execute("SELECT * FROM oams.workforce_members WHERE member_id = %s FOR UPDATE", (member_id,)).fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="member not found")
        updated = conn.execute(
            """UPDATE oams.workforce_members SET employment_status = %s,
                      departed_at = CASE WHEN %s = 'departed' THEN CURRENT_DATE
                                         WHEN %s = 'active' THEN NULL ELSE departed_at END,
                      token_version = token_version + 1, updated_at = now()
                WHERE member_id = %s
                RETURNING member_id, display_name, employment_status, departed_at, token_version""",
            (payload.employment_status, payload.employment_status, payload.employment_status, member_id),
        ).fetchone()
        conn.execute("UPDATE oams.workforce_sessions SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (member_id,))
        if payload.employment_status in ("departed", "suspended"):
            if payload.employment_status == "departed":
                conn.execute("UPDATE oams.permission_grants SET revoked_at = now() WHERE member_id = %s AND revoked_at IS NULL", (member_id,))
                conn.execute(
                    """UPDATE oams.position_assignments SET status = 'ended', ends_at = LEAST(COALESCE(ends_at, now()), now()), ended_at = now(), ended_by = %s
                       WHERE member_id = %s AND status = 'active' AND starts_at < now()""",
                    (actor["member_id"], member_id),
                )
                conn.execute(
                    """UPDATE oams.position_assignments SET status = 'cancelled'
                       WHERE member_id = %s AND status = 'scheduled'""", (member_id,)
                )
        audit(conn, actor["member_id"], "member.status_changed", "member", member_id, payload.reason, updated, member)
        return updated


@app.post("/v1/grants/{grant_id}/revoke")
def revoke_grant(grant_id: UUID, reason: str,
                 actor: dict = Depends(require_permission("workforce.grants", "manage"))):
    with connection() as conn:
        prior = conn.execute("SELECT * FROM oams.permission_grants WHERE grant_id = %s AND revoked_at IS NULL FOR UPDATE", (grant_id,)).fetchone()
        if not prior:
            raise HTTPException(status_code=404, detail="active grant not found")
        row = conn.execute(
            """UPDATE oams.permission_grants SET revoked_at = now()
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
    with connection() as conn:
        if org_unit_id is not None:
            require_org_unit_access(conn, _actor["member_id"], org_unit_id)
        if not has_company_scope(conn, _actor["member_id"]):
            scoped_orgs = list(active_org_unit_ids(conn, _actor["member_id"]))
            if not scoped_orgs:
                return []
            filters.append("w.org_unit_id = ANY(%s)")
            params.append(scoped_orgs)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        return conn.execute(
            f"""SELECT {WORK_ITEM_SELECT}
                  FROM oams.work_items w
                  JOIN oams.positions p USING (position_id)
                  JOIN oams.org_units o ON o.org_unit_id = w.org_unit_id
                  {where}
                 ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                          w.needs_decision DESC, w.updated_at DESC""",
            params,
        ).fetchall()


@app.post("/v1/work-items", status_code=status.HTTP_201_CREATED)
def create_work_item(payload: WorkItemCreate, actor: dict = Depends(require_permission("workforce.work_items", "manage"))):
    try:
        with connection() as conn:
            position = conn.execute("SELECT org_unit_id FROM oams.positions WHERE position_id = %s",
                                    (payload.position_id,)).fetchone()
            if position:
                require_org_unit_access(conn, actor["member_id"], position["org_unit_id"])
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
                "SELECT * FROM oams.work_items WHERE work_item_id = %s FOR UPDATE", (work_item_id,)
            ).fetchone()
            if not prior:
                raise HTTPException(status_code=404, detail="work item not found")
            require_org_unit_access(conn, actor["member_id"], prior["org_unit_id"])
            if "status" in updates and updates["status"] == "blocked":
                reason = updates.get("blocked_reason", prior["blocked_reason"])
                if not (reason and reason.strip()):
                    raise HTTPException(status_code=422, detail="blocked work items require a non-empty blocked_reason")
            column_map = {"title": "title", "status": "status", "blocked_reason": "blocked_reason",
                          "needs_decision": "needs_decision"}
            assignments = ", ".join(f"{column_map[field]} = %s" for field in updates)
            params = list(updates.values()) + [work_item_id]
            row = conn.execute(
                f"""UPDATE oams.work_items SET {assignments}, updated_at = now()
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
    return create_report(work_item_id, payload, actor["member_id"])


def create_report(work_item_id: UUID, payload: ReportCreate, actor_member_id: Any):
    """Share the audited write between the browser and JSON API."""
    try:
        with connection() as conn:
            item = conn.execute(
                "SELECT work_item_id, status FROM oams.work_items WHERE work_item_id = %s FOR UPDATE",
                (work_item_id,),
            ).fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="work item not found")
            if item["status"] not in OPEN_ITEM_STATUSES:
                raise HTTPException(status_code=409, detail="cannot report progress on a closed work item")
            assigned = conn.execute(
                """SELECT 1 FROM oams.position_assignments
                   WHERE member_id = %s AND position_id = (SELECT position_id FROM oams.work_items WHERE work_item_id = %s)
                     AND status IN ('active', 'scheduled') AND starts_at <= now()
                     AND (ends_at IS NULL OR now() < ends_at)""",
                (actor_member_id, work_item_id),
            ).fetchone()
            # Only an explicit security administrator can report outside their
            # assigned position; work-item managers are still position-bound.
            if not assigned and not has_permission(conn, actor_member_id, "workforce.grants", "manage"):
                raise HTTPException(status_code=403, detail="reporter is not assigned to this work item position")
            row = conn.execute(
                """INSERT INTO oams.work_item_reports
                   (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING report_id, work_item_id, reported_by_member_id, submitted_via, progress_value,
                             note, evidence, status, reported_at""",
                (work_item_id, actor_member_id, payload.submitted_via, payload.progress_value,
                 payload.note, payload.evidence),
            ).fetchone()
            conn.execute("UPDATE oams.work_items SET updated_at = now() WHERE work_item_id = %s", (work_item_id,))
            audit(conn, actor_member_id, "work_item_report.created", "work_item_report", row["report_id"],
                  f"progress submitted via {payload.submitted_via}", row)
            return row
    except CheckViolation:
        raise HTTPException(status_code=422, detail="evidence is required and progress_value must be between 0 and 100")


@app.get("/v1/work-items/{work_item_id}/reports")
def list_work_item_reports(work_item_id: UUID,
                           actor: dict = Depends(require_permission("workforce.reports", "read"))):
    with connection() as conn:
        exists = conn.execute("SELECT org_unit_id FROM oams.work_items WHERE work_item_id = %s", (work_item_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="work item not found")
        require_org_unit_access(conn, actor["member_id"], exists["org_unit_id"])
        return conn.execute(
            """SELECT r.report_id, r.reported_by_member_id, m.display_name AS reported_by, r.submitted_via,
                      r.progress_value, r.note, r.evidence, r.status, r.retracted_at, r.reported_at
               FROM oams.work_item_reports r
               JOIN oams.workforce_members m ON m.member_id = r.reported_by_member_id
               WHERE r.work_item_id = %s ORDER BY r.reported_at DESC""",
            (work_item_id,),
        ).fetchall()


@app.post("/v1/reports/{report_id}/retract")
def retract_report(report_id: UUID, payload: ReportRetract,
                   actor: dict = Depends(require_permission("workforce.reports", "submit"))):
    with connection() as conn:
        prior = conn.execute(
            "SELECT * FROM oams.work_item_reports WHERE report_id = %s FOR UPDATE", (report_id,)
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
            """UPDATE oams.work_item_reports
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
                        (SELECT count(*) FROM oams.position_assignments a
                          WHERE a.position_id = p.position_id
                            AND a.status IN ('active', 'scheduled')
                            AND a.starts_at <= now()
                            AND (a.ends_at IS NULL OR now() < a.ends_at)) AS holder_count,
                        s.latest_report_id, s.latest_submitted_via, s.latest_progress_value,
                        s.latest_note, s.latest_evidence, s.latest_reported_at
                   FROM oams.work_items w
                   JOIN oams.positions p ON p.position_id = w.position_id
                   LEFT JOIN oams.work_item_current_state s ON s.work_item_id = w.work_item_id
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


# Stages that can be used as a second filter inside a layer. 'done' is a count
# and not a list -- BOARD_QUERY deliberately excludes finished work, so asking for
# it as a stage could only ever answer with "these zero cards".
SELECTABLE_STAGES: tuple[str, ...] = ("planned", "in_progress", "blocked")

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


def normalize_layer(value: str | None) -> str:
    """Unknown layer ids fall back to 'all' rather than rendering an empty board."""
    known = {layer_id for layer_id, _, _ in BOARD_LAYERS}
    return value if value in known else "all"


def normalize_stage(value: str | None) -> str | None:
    """Unknown stage ids fall back to no stage filter, i.e. the whole layer."""
    return value if value in SELECTABLE_STAGES else None


def select_cards(cards, layer: str, stage: str | None) -> list[dict[str, Any]]:
    """The one predicate the page and the JSON endpoint both use. A stage narrows a
    layer rather than replacing it: the two filters stack, they do not compete."""
    layer = normalize_layer(layer)
    stage = normalize_stage(stage)
    return [card for card in cards
            if layer_matches(layer, card) and (stage is None or card["status"] == stage)]


def build_board(conn, org_unit_id: UUID, stale_days: int = 7, layer: str = "all",
                stage: str | None = None) -> dict[str, Any]:
    """The one board aggregation. Both the JSON endpoint and the HTML board render
    from this, so the page can never drift from the API it is supposed to show."""
    stale_days = min(max(stale_days, 1), 365)
    now = datetime.now(timezone.utc)
    stale_after = timedelta(days=stale_days)
    org_unit = conn.execute(
        "SELECT org_unit_id, parent_org_unit_id, org_code, name, unit_type, status FROM oams.org_units WHERE org_unit_id = %s",
        (org_unit_id,),
    ).fetchone()
    if not org_unit:
        raise HTTPException(status_code=404, detail="org unit not found")
    layer = normalize_layer(layer)
    stage = normalize_stage(stage)
    rows = conn.execute(BOARD_QUERY, (org_unit_id,)).fetchall()
    child_org_units = conn.execute(
        """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
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

    # A layer badge answers "how many would I see if I clicked you", so it counts
    # inside the stage already selected -- otherwise clicking could never deliver
    # the number on the tab. The stage strip does the mirror image: `flow` counts
    # ignore the stage filter and honour the layer. Both readings meet in the list.
    layers = [{"id": layer_id, "label": label, "hint": hint,
               "count": len(select_cards(cards, layer_id, stage))}
              for layer_id, label, hint in BOARD_LAYERS]
    visible_cards = select_cards(cards, layer, stage)

    # The flow is one strip of counters, not a second board: every stage count is
    # measured inside the current layer, so the three selectable stages always add
    # up to the list below. "done" is the one exception and says so out loud --
    # BOARD_QUERY excludes finished work on purpose, so it stays a department-wide
    # count instead of pretending to be a layer of cards.
    finished = conn.execute(
        """SELECT count(*) AS n FROM oams.work_items
           WHERE org_unit_id = %s AND status IN ('done', 'cancelled')""",
        (org_unit_id,),
    ).fetchone()["n"]
    flow = []
    for stage_id, label in FLOW_STAGES:
        if stage_id == "done":
            flow.append({"id": stage_id, "label": label, "count": finished, "selectable": False,
                         "branch": False, "note": "部门累计，明细见工作项列表"})
            continue
        stage_cards = [card for card in cards if card["status"] == stage_id]
        flow.append({"id": stage_id, "label": label,
                     "count": sum(1 for card in stage_cards if layer_matches(layer, card)),
                     "selectable": True, "branch": stage_id == "blocked",
                     "note": "进行中卡住的分支，不是必经阶段" if stage_id == "blocked" else None})

    return {
        "org_unit": org_unit,
        "generated_at": now,
        "stale_days": stale_days,
        "layer": layer,
        "stage": stage,
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
        "visible_cards": visible_cards,
        "sections": {
            "blocked": [card for card in cards if card["status"] == "blocked"],
            "needs_decision": [card for card in cards if card["needs_decision"] and card["status"] != "blocked"],
            "by_position": sorted(by_position.values(), key=lambda bucket: bucket["position"]["position_code"]),
        },
        "child_org_units": child_org_units,
    }


@app.get("/v1/board/org-units/{org_unit_id}")
def board_for_org_unit(org_unit_id: UUID, stale_days: int = 7, layer: str = "all",
                       stage: str | None = None,
                       actor: dict = Depends(require_permission("workforce.work_items", "read"))):
    with connection() as conn:
        require_org_unit_access(conn, actor["member_id"], org_unit_id)
        return build_board(conn, org_unit_id, stale_days, layer, stage)


def insert_work_item(conn, payload: WorkItemCreate, actor_member_id: Any) -> dict[str, Any]:
    """Shared by the JSON endpoint and the admin page; one insert, one audit."""
    position = conn.execute(
        "SELECT p.position_id, p.org_unit_id, p.status FROM oams.positions p WHERE p.position_id = %s",
        (payload.position_id,),
    ).fetchone()
    if not position:
        raise HTTPException(status_code=404, detail="position not found")
    if position["status"] != "active":
        raise HTTPException(status_code=409, detail="work items can only be attached to an active position")
    row = conn.execute(
        """INSERT INTO oams.work_items
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
        """INSERT INTO oams.positions (position_code, title, org_unit_id)
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
               FROM oams.workforce_audit_events ORDER BY occurred_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()


@app.post("/v1/sessions/current/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_current_session(principal: dict = Depends(current_principal)):
    # Shortcut principals carry no session row; there is nothing to revoke.
    if principal.get("session_id") is not None:
        with connection() as conn:
            revoke_session(conn, principal["session_id"], principal["member_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Department entries -> company rollup
#
# Until now the two halves of the product never touched: a department filed
# interface_entries, the company board read work_items, and nothing ever wrote
# interface_entries.work_item_id. So a department's numbers could not reach the
# company however honest the form was (plugin-host-concept ch.2).
#
# The rule below is the contract's single condition set (ch.6): an entry feeds a
# company number only when
#     status_entry = 'active' AND entry_mode = 'live' AND work_item_id IS NOT NULL
# It is written once, in ROLLUP_PREDICATE / ROLLUP_STATE_SQL, so the badge on a
# department page, the board panel and the JSON endpoint can never disagree about
# which entries counted. Draft data and unlinked entries never enter a rollup.
# ---------------------------------------------------------------------------

#: The only entries that may feed a company number.
ROLLUP_PREDICATE = ("e.status_entry = 'active' AND e.entry_mode = 'live' "
                    "AND e.work_item_id IS NOT NULL")

#: Why an entry is (not) in the rollup, as one SQL CASE shared by every reader.
ROLLUP_STATE_SQL = ("CASE WHEN e.status_entry <> 'active' THEN 'retracted' "
                    "WHEN e.entry_mode <> 'live' THEN 'draft' "
                    "WHEN e.work_item_id IS NULL THEN 'unlinked' "
                    "ELSE 'rolled' END")

ROLLUP_STATES = ("rolled", "unlinked", "draft", "retracted")

# ---------------------------------------------------------------------------
# 部门自己那一面（页面面）
#
# The predicate above answers a *company* question -- what may lift a company
# number -- so it is deliberately strict: a ledger row that was never planned as
# a work item must not inflate the company total.
#
# A department reading its own page asks a different question: "what did we
# record?" A reimbursed expense does not need a project before it can be counted,
# so the department side drops the work_item_id half and keeps only the two
# conditions that say the entry is real (not draft, not retracted).
#
# The contract (ch.6) states the consequence as a rule rather than leaving it to
# be discovered: one entry counts on its department page and does not count in the
# company rollup until it is linked. `unlinked` means "not in the company number",
# never "not real". Anything that reads a department number uses
# DEPARTMENT_ROLLUP_*; anything that reads a company number keeps ROLLUP_*.
# ---------------------------------------------------------------------------

#: The entries that feed a department page's own numbers.
DEPARTMENT_ROLLUP_PREDICATE = "e.status_entry = 'active' AND e.entry_mode = 'live'"

#: Why an entry is (not) in a department number. `counted_unlinked` says the entry
#: counts here but would not reach the company until somebody links a work item.
DEPARTMENT_ROLLUP_STATE_SQL = ("CASE WHEN e.status_entry <> 'active' THEN 'retracted' "
                               "WHEN e.entry_mode <> 'live' THEN 'draft' "
                               "WHEN e.work_item_id IS NULL THEN 'counted_unlinked' "
                               "ELSE 'counted' END")

DEPARTMENT_ROLLUP_STATES = ("counted", "counted_unlinked", "draft", "retracted")

#: Whether one entry would reach the company rollup, as a plain boolean column.
FEEDS_COMPANY_SQL = ("(e.status_entry = 'active' AND e.entry_mode = 'live' "
                     "AND e.work_item_id IS NOT NULL)")

#: The standard columns a declared field may map onto. Aggregation reads these;
#: a display_only field has no column and stays private to the department.
ROLLUP_COLUMNS = ("title", "status", "blocked_reason", "progress_value", "note", "evidence")

#: Standard fields that have no column of their own and land in `extra` instead.
#: They still aggregate: a department's money (amount) is a real number its page
#: must be able to show, and "the field exists but cannot be summed" is not an
#: answer a department head accepts. Reading is TEXT-safe — _aggregate_value
#: casts, and a non-numeric value is skipped rather than turned into 0.
EXTRA_AGGREGATE_COLUMNS = ("amount",)

ENTRY_COLUMNS_SQL = ("e.entry_id, e.interface_id, e.work_item_id, e.title, e.status, "
                     "e.blocked_reason, e.progress_value, e.note, e.evidence, e.extra, "
                     "e.reported_by, e.reported_at, e.entry_mode, e.status_entry")

#: Which entries belong to *the page* as opposed to one version row of it.
#:
#: `interface_entries.interface_id` points at a version, and every re-publish
#: inserts a new version row (the identity of a page is `(org_unit_id, slug)`,
#: enforced by two partial unique indexes). Reading numbers per version therefore
#: dropped whatever was recorded before the last publish: on the dev database seven
#: rows were already invisible to the total, and the same rows were unreachable
#: from the workbench. This scope resolves the page from whichever version id the
#: caller holds and matches every version of it, so a ledger row keeps counting
#: after the page is re-published.
PAGE_ENTRY_SCOPE_SQL = (
    "e.interface_id IN (SELECT v.interface_id FROM oams.interfaces v "
    " WHERE (v.org_unit_id, v.slug) = (SELECT w.org_unit_id, w.slug "
    "   FROM oams.interfaces w WHERE w.interface_id = %s))")


def _distinct_entries(rows):
    """Rows of the same entry exactly once, keeping the first (newest) one.

    One entry row lives on one version, so the page scope above cannot return it
    twice; this is here because "sum it once" is the promise a number makes, and a
    join that grows later must not silently double a total.
    """
    seen, out = set(), []
    for row in rows:
        key = row.get("entry_id")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _reason(exc):
    """One short sentence, never a stack trace: the tone the metrics panel uses."""
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def _descriptor_bindings(descriptor):
    """Emit-mode metric bindings of one descriptor, defensively.

    Display bindings are skipped on purpose: they show a platform metric and
    carry no aggregation of their own (contract 2), so they can never answer
    "which entries produced this number".
    """
    raw = descriptor.get("metrics") if isinstance(descriptor, dict) else None
    if not isinstance(raw, list):
        return []
    bindings = []
    for binding in raw:
        if isinstance(binding, dict) and binding.get("mode") == "emit" and binding.get("metric_key"):
            bindings.append(binding)
    return bindings


def _field_map_to(descriptor, field_key):
    """The standard column a declared field writes to (None for display_only)."""
    if not field_key or not isinstance(descriptor, dict):
        return None
    for field in descriptor.get("fields") or []:
        if isinstance(field, dict) and field.get("key") == field_key:
            map_to = field.get("map_to")
            if map_to in ROLLUP_COLUMNS or map_to in EXTRA_AGGREGATE_COLUMNS:
                return map_to
            return None
    return None


def _field_label(descriptor, field_key):
    if not field_key or not isinstance(descriptor, dict):
        return None
    for field in descriptor.get("fields") or []:
        if isinstance(field, dict) and field.get("key") == field_key:
            label = field.get("label")
            return str(label) if label else None
    return None


def _binding_label(descriptor, binding):
    """What to call this metric on a page: 声明方写的 `label` 优先，其次是字段名，最后才是键。

    名字属声明方（emit 绑定，契约 §2/§7）；键名 `dept.OPS.weekly_done` 是给机器看的，
    只有前两条都拿不到时才拿它兜底——总比空白好，但页面上不该出现它。
    """
    if not isinstance(binding, dict):
        return None
    declared = binding.get("label")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return (_field_label(descriptor, binding.get("from_field"))
            or binding.get("metric_key") or None)


def _binding_unit(binding):
    """Unit the declaration carried, or "" — 空串合法，不猜。"""
    if not isinstance(binding, dict):
        return ""
    unit = binding.get("unit")
    return unit.strip() if isinstance(unit, str) else ""


INTERFACES_SQL = """
    SELECT i.interface_id, i.name, i.org_unit_id, i.version_no, i.descriptor,
           o.org_code, o.name AS org_unit_name
      FROM oams.interfaces i
      JOIN oams.org_units o ON o.org_unit_id = i.org_unit_id
     WHERE i.status = 'active'
       AND (%(scope)s)"""


def _active_interfaces(conn, org_unit_id, interface_id=None):
    """The pages whose numbers may enter a rollup (only approved pages count).

    A department aggregates its own approved pages plus those of its child
    departments. A company aggregates every active department: the company board
    is supposed to answer "what did the departments report", and a department
    whose parent pointer was never filled in must still be visible there.
    """
    unit = conn.execute(
        "SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units WHERE org_unit_id = %s",
        (org_unit_id,)).fetchone()
    is_company = bool(unit) and (unit.get("unit_type") or "") == "company"
    params = {}
    if is_company:
        scope_sql = ("i.org_unit_id IN (SELECT org_unit_id FROM oams.org_units "
                     "WHERE status = 'active' AND unit_type <> 'company')")
    else:
        scope_sql = ("(i.org_unit_id = %(unit)s OR i.org_unit_id IN "
                     "(SELECT org_unit_id FROM oams.org_units "
                     " WHERE parent_org_unit_id = %(unit)s AND status = 'active'))")
        params["unit"] = org_unit_id
    sql = INTERFACES_SQL % {"scope": scope_sql}
    if interface_id is not None:
        sql += " AND i.interface_id = %(interface)s"
        params["interface"] = interface_id
    sql += " ORDER BY o.name, i.name, i.version_no DESC"
    return conn.execute(sql, params).fetchall()


def _binding_entries(conn, interface_id, binding, company=False):
    """Entries of one page that satisfy the predicate (and where_status).

    ``company=False`` is the department side: an entry counts as soon as it is
    real, so a ledger row does not need a work item first. ``company=True`` is the
    strict side used when a number is meant to speak for the whole company.

    Scoped to the page, not to the version row the caller happens to hold (see
    PAGE_ENTRY_SCOPE_SQL): re-publishing a page must not make its earlier entries
    disappear from its own numbers.
    """
    if company:
        predicate, state_sql = ROLLUP_PREDICATE, ROLLUP_STATE_SQL
    else:
        predicate, state_sql = DEPARTMENT_ROLLUP_PREDICATE, DEPARTMENT_ROLLUP_STATE_SQL
    sql = ("SELECT " + ENTRY_COLUMNS_SQL + ", " + state_sql + " AS rollup_state, "
           + FEEDS_COMPANY_SQL + " AS feeds_company "
           "FROM oams.interface_entries e WHERE " + PAGE_ENTRY_SCOPE_SQL + " AND " + predicate)
    params = [interface_id]
    where_status = binding.get("where_status")
    if where_status:
        sql += " AND e.status = %s"
        params.append(where_status)
    sql += " ORDER BY e.reported_at DESC, e.entry_id DESC"
    return _distinct_entries(conn.execute(sql, tuple(params)).fetchall())


def page_entries(conn, interface_id):
    """Every stored entry of a page with its department state, newest first.

    This is what the fill page lists: including the entries that are NOT counted,
    because the point of the badge is that people can see, per entry, why a number
    is smaller than the work they did. ``feeds_company`` travels alongside so the
    same row can say "counts here, not in the company number yet".

    Page-scoped like the numbers (PAGE_ENTRY_SCOPE_SQL): an entry recorded on an
    earlier version is still this page's entry, and the workbench is where people
    go to link or retract it.
    """
    rows = conn.execute(
        "SELECT " + ENTRY_COLUMNS_SQL + ", " + DEPARTMENT_ROLLUP_STATE_SQL + " AS rollup_state, "
        + FEEDS_COMPANY_SQL + " AS feeds_company "
        "FROM oams.interface_entries e WHERE " + PAGE_ENTRY_SCOPE_SQL + " "
        "ORDER BY e.reported_at DESC, e.entry_id DESC", (interface_id,)).fetchall()
    return _distinct_entries(rows)


def _column_value(row, column):
    """One row's value for an aggregation column.

    Columns are the standard ones; a few standard fields (money: `amount`) have
    no column of their own and live in `extra`, so both places are looked up.
    """
    value = row.get(column)
    if value is not None:
        return value
    extra = row.get("extra")
    if isinstance(extra, dict):
        return extra.get(column)
    return None


def _aggregate_values(agg, values):
    """count_distinct / sum / avg over values already pulled off the rows.

    单页与多页两个入口共用这一段算术：两处各写一遍时，改一边不改另一边不会
    报错（2026-11-02 OAMS测试 用变异证明过），所以算术只留一份。
    `count` 数的是行、与列值是否为 NULL 无关，由调用方自己算。

    NULL 在这里是跳过，不当 0；一个数都读不出来时给 None —— 空与零是两件事
    （契约 §6.1）。
    """
    present = [value for value in values if value is not None]
    if agg == "count_distinct":
        return len({str(value) for value in present})
    numbers = []
    for value in present:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return None
    if agg == "sum":
        total = sum(numbers)
        return int(total) if float(total).is_integer() else total
    if agg == "avg":
        return sum(numbers) / len(numbers)
    return None


def _aggregate_value(agg, column, rows):
    """count / sum / avg / count_distinct over one column; NULLs are skipped."""
    if agg == "count":
        return len(rows)
    return _aggregate_values(agg, [_column_value(row, column) for row in rows])


def _combine_pages(agg, page_rows):
    """Fold several pages' rows into one number without double counting.

    page_rows is [(column, rows)] -- one pair per page that could resolve its
    from_field. The rows are pooled *before* the arithmetic because per-page
    arithmetic does not compose:

    * count       -- the rows themselves, so two pages both count;
    * sum         -- the measured values added across pages;
    * avg         -- the pooled mean, which weights a page by its row count; the
                     mean of the page means would let a page with one entry
                     outvote a page with fifty;
    * count_distinct -- the union of the values, because a value reported on two
                     pages is still one value. Adding the per-page distinct
                     counts would count it twice.
    """
    if not page_rows:
        # No page could resolve its from_field to a column the rollup owns, so
        # there is nothing to count -- return "unknown", not zero.
        return None
    if agg == "count":
        return sum(len(rows) for _, rows in page_rows)
    return _aggregate_values(
        agg,
        [_column_value(row, column) for column, rows in page_rows for row in rows],
    )


def _format_metric(metric_key, value, unit=""):
    """The shared formatter, so a department number reads like any other one.

    单位由调用方给：emit 绑定可选的 `unit` 原样传进来（`_binding_unit`），没写就传空串
    ——不带单位是允许的显示结果，不猜。声明方写单位这条口径见 p0-interface-contract §2/§7。
    """
    try:
        from oams.metric_format import format_metric
        return format_metric(metric_key, value, unit or "")
    except Exception:  # noqa: BLE001 - formatting is never worth a 500
        return "—" if value is None else str(value)


def entry_state_counts(conn, interface_id):
    """How many entries of this page sit in each department state.

    Page-scoped like the numbers and the list (PAGE_ENTRY_SCOPE_SQL): the counts
    line and the table beside it must describe the same set of entries, otherwise
    the page says "计入 3 条" above a table showing one.
    """
    rows = conn.execute(
        "SELECT state, count(*) AS n FROM (SELECT " + DEPARTMENT_ROLLUP_STATE_SQL + " AS state "
        "FROM oams.interface_entries e WHERE " + PAGE_ENTRY_SCOPE_SQL + ") t GROUP BY state",
        (interface_id,)).fetchall()
    counts = {state: 0 for state in DEPARTMENT_ROLLUP_STATES}
    for row in rows:
        state = row.get("state")
        if state in counts:
            counts[state] = int(row.get("n") or 0)
    return counts


def _entry_brief(row):
    return {
        "entry_id": row.get("entry_id"),
        "title": row.get("title"),
        "status": row.get("status"),
        "progress_value": row.get("progress_value"),
        "reported_at": row.get("reported_at"),
        "work_item_id": row.get("work_item_id"),
    }


def rollup_entries(conn, interface_id, metric_key=None):
    """The entries that actually participated in a number (or in the page).

    Never raises: a missing page, an undeclared metric or a broken descriptor is
    an unavailable answer with a reason, because this is what a manager opens
    when a number looks wrong -- and a 500 would hide the explanation.
    """
    try:
        page = conn.execute(
            """SELECT interface_id, name, org_unit_id, status, descriptor
                 FROM oams.interfaces WHERE interface_id = %s""",
            (interface_id,)).fetchone()
        if not page:
            return {"available": False, "reason": "找不到这个界面", "entries": [],
                    "metric_key": metric_key, "value": None}
        base = {"available": True, "reason": None, "interface_id": page["interface_id"],
                "interface_name": page["name"], "metric_key": metric_key,
                "mode": None, "agg": None, "from_field": None, "where_status": None}
        binding = None
        if metric_key:
            binding = next((b for b in _descriptor_bindings(page["descriptor"])
                            if b.get("metric_key") == metric_key), None)
            if binding is None:
                base.update({
                    "available": False,
                    "reason": "这个页面没有声明指标 " + str(metric_key) + "，因此没有条目参与它",
                    "value": None, "entries": [], "entry_count": 0})
                return base
            rows = _binding_entries(conn, page["interface_id"], binding, company=True)
        else:
            rows = conn.execute(
                "SELECT " + ENTRY_COLUMNS_SQL + ", " + ROLLUP_STATE_SQL + " AS rollup_state "
                "FROM oams.interface_entries e WHERE e.interface_id = %s AND "
                + ROLLUP_PREDICATE + " ORDER BY e.reported_at DESC, e.entry_id DESC",
                (page["interface_id"],)).fetchall()
        column = _field_map_to(page["descriptor"], (binding or {}).get("from_field"))
        value = None
        if binding is not None and column is not None:
            value = _aggregate_value(binding.get("agg"), column, rows)
        if binding is not None:
            base.update({"mode": "emit", "agg": binding.get("agg"),
                         "from_field": binding.get("from_field"),
                         "where_status": binding.get("where_status")})
        base.update({
            "value": value,
            "formatted": _format_metric(metric_key, value, _binding_unit(binding)) if metric_key else None,
            "entry_count": len(rows),
            "entries": [_entry_brief(row) for row in rows],
        })
        return base
    except Exception as exc:  # noqa: BLE001 - a rollup lookup must never 500 a page
        return {"available": False, "reason": _reason(exc), "entries": [],
                "metric_key": metric_key, "value": None}


def aggregate_entries(conn, org_unit_id, metric_key, interface_id=None):
    """One emitted metric, computed from the entries that are allowed to count.

    Contract 2 binding: {metric_key, mode=emit, agg, from_field, where_status}.
    The binding is read from the page's own descriptor (never re-invented here),
    and the number is computed over entries passing DEPARTMENT_ROLLUP_PREDICATE:
    this is a department's number, so an unlinked ledger row still counts (see the
    comment on that constant). Company totals keep the stricter ROLLUP_PREDICATE.
    """
    try:
        # Every page that declares this metric contributes; the schema allows two
        # active pages of one department to declare the same key (the unique index
        # is on slug, not metric_key), and silently taking the first one would make
        # the company number smaller than the work the departments did.
        hits = []
        for page in _active_interfaces(conn, org_unit_id, interface_id):
            for binding in _descriptor_bindings(page["descriptor"]):
                if binding.get("metric_key") == metric_key:
                    hits.append((page, binding))
        if not hits:
            return {"available": False, "reason": "没有已启用的页面声明指标 " + str(metric_key),
                    "metric_key": metric_key, "value": None, "entries": [], "entry_count": 0}
        # Every page's rows are pooled before the arithmetic runs: taking the
        # first page's value, averaging the page means, or adding up per-page
        # distinct counts would all be wrong once two pages declare one key.
        first_page, first_binding = hits[0]
        agg = first_binding.get("agg")
        entries = []
        per_page = []
        page_rows = []
        for page, binding in hits:
            column = _field_map_to(page["descriptor"], binding.get("from_field"))
            rows = _binding_entries(conn, page["interface_id"], binding)
            value = None if column is None else _aggregate_value(binding.get("agg"), column, rows)
            if column is not None:
                page_rows.append((column, rows))
            entries.extend(_entry_brief(row) for row in rows)
            per_page.append({
                "interface_id": page["interface_id"],
                "interface_name": page["name"],
                "org_unit_name": page.get("org_unit_name"),
                "entry_count": len(rows),
                "value": value,
            })
        total = _combine_pages(agg, page_rows)
        usable = bool(page_rows)
        return {
            "available": True,
            "reason": None if usable else "指标读取的字段只在本部门可见，不参与汇总",
            "metric_key": metric_key,
            "mode": "emit",
            "agg": agg,
            "from_field": first_binding.get("from_field"),
            "where_status": first_binding.get("where_status"),
            "label": _binding_label(first_page["descriptor"], first_binding),
            "interface_id": first_page["interface_id"],
            "interface_name": first_page["name"],
            "org_unit_id": first_page["org_unit_id"],
            "org_unit_name": first_page.get("org_unit_name"),
            "pages": per_page,
            "value": total,
            "formatted": _format_metric(metric_key, total),
            "entry_count": len(entries),
            "entries": entries,
        }
    except Exception as exc:  # noqa: BLE001 - see rollup_entries
        return {"available": False, "reason": _reason(exc), "metric_key": metric_key,
                "value": None, "entries": [], "entry_count": 0}


def build_department_aggregates(conn, org_unit_id, descriptor=None, interface_id=None):
    """Everything a department page and a board panel need about its numbers.

    Returns the declared metrics with real values, the one dict render_interface
    wants for metric_card / metric_grid (``aggregates``), and how many entries sit
    in each rollup state -- so the page can say out loud why a number is small
    instead of leaving people to guess. Total: a failure is available=False with
    a reason, never an exception, because this runs inside a page render.
    """
    empty = {"available": False, "reason": None, "org_unit_id": str(org_unit_id),
             "interfaces": [], "metrics": [], "aggregates": {},
             "counts": {state: 0 for state in DEPARTMENT_ROLLUP_STATES}, "total": 0}
    try:
        pages = list(_active_interfaces(conn, org_unit_id, interface_id))
        if descriptor is not None and not pages:
            pages = [{"interface_id": interface_id, "name": None, "descriptor": descriptor}]
        panel = dict(empty)
        panel.update({"available": True, "interfaces": [], "metrics": [], "aggregates": {}})
        totals = {state: 0 for state in DEPARTMENT_ROLLUP_STATES}
        for page in pages:
            page_interface_id = page.get("interface_id")
            counts = (entry_state_counts(conn, page_interface_id) if page_interface_id is not None
                      else {state: 0 for state in DEPARTMENT_ROLLUP_STATES})
            for state, count in counts.items():
                totals[state] = totals.get(state, 0) + count
            page_metrics = []
            for binding in _descriptor_bindings(page.get("descriptor")):
                column = _field_map_to(page.get("descriptor"), binding.get("from_field"))
                rows = (_binding_entries(conn, page_interface_id, binding)
                        if page_interface_id is not None else [])
                value = None if column is None else _aggregate_value(binding.get("agg"), column, rows)
                metric = {
                    "metric_key": binding.get("metric_key"),
                    "label": _binding_label(page.get("descriptor"), binding),
                    "mode": "emit",
                    "agg": binding.get("agg"),
                    "from_field": binding.get("from_field"),
                    "where_status": binding.get("where_status"),
                    "value": value,
                    "formatted": _format_metric(binding.get("metric_key"), value,
                                                _binding_unit(binding)),
                    "entry_count": len(rows),
                    "interface_id": page_interface_id,
                    "interface_name": page.get("name"),
                    "org_unit_id": page.get("org_unit_id"),
                    "org_unit_name": page.get("org_unit_name"),
                    "available": column is not None,
                    "reason": None if column is not None else "字段只在本部门可见，不参与汇总",
                }
                page_metrics.append(metric)
                panel["metrics"].append(metric)
                if column is not None:
                    panel["aggregates"][binding.get("metric_key")] = value
            panel["interfaces"].append({
                "interface_id": page_interface_id,
                "name": page.get("name"),
                "org_unit_id": page.get("org_unit_id"),
                "org_unit_name": page.get("org_unit_name"),
                "counts": counts,
                "entry_count": sum(counts.values()),
                "metrics": page_metrics,
            })
        panel["counts"] = totals
        panel["total"] = sum(totals.values())
        return panel
    except Exception as exc:  # noqa: BLE001 - degrade, never 500 a board
        empty["reason"] = _reason(exc)
        return empty


def list_open_work_items(conn, org_unit_id=None, company_scope=False, limit=100):
    """The work items an entry may be attached to: open ones, in scope.

    Finished and cancelled items are excluded: linking to one would let a number
    outlive the work it describes. Company scope sees every department; a member
    of one department sees their own.
    """
    # A non-numeric limit (None, "abc") is a caller bug, not a 500: clamping a
    # string raises ValueError before the comparison ever runs.
    try:
        limit = min(max(int(limit), 1), 500)
    except (TypeError, ValueError):
        limit = 100
    sql = ("SELECT w.work_item_id, w.title, w.status, w.org_unit_id, o.name AS org_unit_name "
           "FROM oams.work_items w JOIN oams.org_units o ON o.org_unit_id = w.org_unit_id "
           "WHERE w.status IN ('planned', 'in_progress', 'blocked')")
    params = []
    if not company_scope:
        if org_unit_id is None:
            return []
        sql += " AND w.org_unit_id = %s"
        params.append(org_unit_id)
    sql += " ORDER BY o.name, w.updated_at DESC LIMIT %s"
    params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


# ---------------------------------------------------------------------------
# Web UI
#
# The pages are mounted on the same app as the JSON API so there is one process to
# run and one session table to reason about. The only thing the UI layer adds is a
# cookie carrier: the token in that cookie is an ordinary session, resolved by
# security.principal_for_token and checked by security.has_permission -- the same
# functions the bearer scheme uses. Nothing here grants anything by itself.
# ---------------------------------------------------------------------------
from oams.web import router as web_router  # noqa: E402
from oams import web as web_ui  # noqa: E402
from oams.security import (active_org_unit_ids, has_company_scope,  # noqa: E402
                              has_permission, principal_from_request)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from oams import collab  # noqa: E402



class _VersionedStaticFiles(StaticFiles):
    """带版本串的静态资源可以长缓存，不带的不许浏览器自己猜。

    URL 上的 `v=` 是文件内容的哈希（`oams.web.static_url` 生成）。只认「跟当前
    文件对得上」的版本串：对得上，说明这个 URL 指向的内容永远不变，可以 immutable；
    对不上或者根本没带（旧缓存里的残页、有人手打的地址），就 `no-cache`——让浏览器
    每次回源问一句。改版之后第一跳看到的就是新样式，不必等缓存过期，也不必让人按
    Ctrl+Shift+R。

    `no-cache` 不是「不缓存」，是「可以存，但每次用之前先问」；StaticFiles 本来就会
    发 ETag 与 Last-Modified，对上了照样 304，代价就是一次条件请求。
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        supplied = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [""])[0]
        if supplied and supplied == web_ui.static_version(path):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _VersionedStaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")
app.include_router(web_router)


def _shell_rail(path, nav, departments, dept_active, caps):
    """The left column is the sub-navigation of whichever top entry you are in.

    A screen can have several views -- the review queue and what it decided
    lately, the setup overview and the audit log that backs it. Those belong
    under the entry that owns them, not in the global bar; putting them in the
    bar made every view look equally important. An item is only offered when
    this person can open it: a link that answers 403 is worse than no link.
    """
    head = path.strip("/").split("/")[0] if path.strip("/") else ""
    items = []

    def add(label, href, on=False):
        items.append({"label": label, "href": href, "on": bool(on)})

    # Building a page is a view of the interface screens, not of setup, so it is
    # listed with them even though its URL still lives under /setup.
    if path.startswith("/setup/interfaces"):
        head = "interfaces"

    if head in ("progress", "departments", "reports"):
        if nav.get("progress"):
            add("公司总览", "/progress", path.startswith("/progress"))
        for row in departments:
            add(row["name"], "/departments/" + str(row["org_unit_id"]),
                dept_active == row["org_unit_id"])
    elif head in ("interfaces", "board"):
        add("汇报汇总", "/interfaces/board", path.startswith("/interfaces/board"))
        if nav.get("departments"):
            add("部门页面", "/interfaces", path == "/interfaces")
        if caps.get("interfaces"):
            add("界面制作", "/setup/interfaces", path.startswith("/setup/interfaces"))
    elif head == "reviews":
        add("待审核", "/reviews", path == "/reviews")
        add("最近决定", "/reviews#decided")
        if nav.get("departments"):
            add("部门页面", "/interfaces")
    elif head in ("settings", "admin", "setup"):
        if caps.get("settings"):
            add("部门与岗位", "/settings", path == "/settings")
        if caps.get("positions"):
            add("岗位目录", "/admin/positions", path.startswith("/admin/positions"))
        if caps.get("work_items"):
            add("工作项管理", "/admin/work-items", path.startswith("/admin/work-items"))
        if caps.get("audit"):
            add("审计记录", "/admin/audit", path == "/admin/audit")
        if caps.get("setup"):
            add("试用与开关", "/setup", path == "/setup")
    return items


@app.middleware("http")
async def attach_browser_principal(request, call_next):
    # Cheap when absent: API callers send no cookie, so no lookup happens at all.
    request.state.member = principal_from_request(request)
    # The navigation is built from what this person can actually do, so nobody is
    # shown a door they cannot open -- and nobody has to guess why it is missing.
    request.state.nav = {"departments": False, "progress": False, "review": False, "admin": False}
    request.state.dept_rail = []
    request.state.rail = []
    request.state.dept_active = None
    # 协作室入口按钮上的未读数。只在整页渲染时算：htmx 片段不画外壳，算了就是白查。
    request.state.collab_unread = 0
    # Highlight the department the URL is inside, so the sidebar shows where you are.
    parts = [p for p in request.url.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "departments":
        try:
            request.state.dept_active = UUID(parts[1])
        except (ValueError, TypeError):
            request.state.dept_active = None
    if request.state.member is not None:
        member_id = request.state.member["member_id"]
        with connection() as conn:
            # 进度中心 and 汇报中心 are open to everybody; the pages themselves
            # decide how much detail each level gets. 页面审核 and 设置 stay behind
            # their own grants.
            nav = {
                "departments": has_permission(conn, member_id, "workforce.reports", "read"),
                "progress": has_permission(conn, member_id, "workforce.work_items", "read"),
                "review": has_permission(conn, member_id, "workforce.interfaces", "review"),
                "admin": has_company_scope(conn, member_id),
            }
            request.state.nav = nav
            # The departments this person may enter, for the progress column.
            if nav["departments"]:
                if has_company_scope(conn, member_id):
                    request.state.dept_rail = conn.execute(
                        """SELECT org_unit_id, name FROM oams.org_units
                           WHERE status = 'active' AND unit_type <> 'company' ORDER BY name""").fetchall()
                else:
                    scoped = list(active_org_unit_ids(conn, member_id))
                    if scoped:
                        request.state.dept_rail = conn.execute(
                            """SELECT org_unit_id, name FROM oams.org_units
                               WHERE status = 'active' AND org_unit_id = ANY(%s) ORDER BY name""",
                            (scoped,)).fetchall()
            caps = {
                "settings": has_permission(conn, member_id, "workforce.grants", "manage"),
                "audit": has_permission(conn, member_id, "workforce.audit", "read"),
                "setup": all(has_permission(conn, member_id, resource, "manage")
                             for resource in ("workforce.grants", "workforce.members",
                                              "workforce.positions", "workforce.assignments")),
                "interfaces": has_permission(conn, member_id, "workforce.interfaces", "manage"),
                "work_items": has_permission(conn, member_id, "workforce.work_items", "read"),
                "positions": has_permission(conn, member_id, "workforce.positions", "read"),
            }
            request.state.rail = _shell_rail(request.url.path, nav, request.state.dept_rail,
                                             request.state.dept_active, caps)
            # 徽章只在整页出现。片段响应（/collab 里那几条）不画外壳，跳过这次查询。
            if request.method == "GET" and not request.url.path.startswith("/collab"):
                request.state.collab_unread = collab.unread_total(conn, member_id)
    return await call_next(request)
