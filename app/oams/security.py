import hashlib
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from oams.config import settings
from oams.db import connection

bearer = HTTPBearer(auto_error=False)


def digest_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_token() -> str:
    return secrets.token_urlsafe(40)


def issue_session(member_id: str, provider: str = "administrator-issued",
                  expires_at: datetime | None = None,
                  reason: str = "由运维命令行签发了登录会话") -> tuple[str, datetime]:
    """Mint a session token.

    ``expires_at`` exists for one caller only: a connected app's ticket must never
    outlive the human's "允许" (app_authorizations.valid_until). Everything else
    keeps the ordinary TTL, so a short-lived ticket cannot become a long one by
    being issued from somewhere else.
    """
    token = new_token()
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    with connection() as conn:
        member = conn.execute(
            "SELECT employment_status, token_version FROM oams.workforce_members WHERE member_id = %s FOR UPDATE",
            (member_id,),
        ).fetchone()
        if not member or member["employment_status"] != "active":
            raise ValueError("member must exist and be active")
        conn.execute(
            "INSERT INTO oams.workforce_sessions (member_id, token_digest, token_version, expires_at, auth_provider) VALUES (%s, %s, %s, %s, %s)",
            (member_id, digest_token(token), member["token_version"], expires_at, provider),
        )
        conn.execute(
            "INSERT INTO oams.workforce_audit_events (event_type, subject_type, subject_id, reason, metadata) VALUES ('session.issued', 'member', %s, %s, %s)",
            (member_id, reason, Jsonb({"provider": provider, "expires_at": expires_at.isoformat()})),
        )
    return token, expires_at


def principal_for_token(token: str) -> dict[str, Any] | None:
    """Resolve a session token, whichever channel it arrived on.

    Both the bearer scheme (MCP, curl) and the browser cookie end up here, so a
    session behaves identically on both: the UI cannot be a softer way in.
    """
    with connection() as conn:
        row = conn.execute(
            """SELECT m.member_id, m.display_name, m.employment_status, m.token_version,
                      s.session_id, s.token_version AS session_token_version,
                      s.auth_provider,
                      (SELECT p.title FROM oams.position_assignments a
                         JOIN oams.positions p USING (position_id)
                        WHERE a.member_id = m.member_id
                          AND a.status IN ('active', 'scheduled')
                          AND a.starts_at <= now()
                          AND (a.ends_at IS NULL OR now() < a.ends_at)
                        ORDER BY a.starts_at DESC LIMIT 1) AS position_title
               FROM oams.workforce_sessions s
               JOIN oams.workforce_members m USING (member_id)
               WHERE s.token_digest = %s AND s.revoked_at IS NULL
                 AND s.expires_at > now() AND m.employment_status = 'active'""",
            (digest_token(token),),
        ).fetchone()
    if not row or row["token_version"] != row["session_token_version"]:
        return None
    return row


# ---------------------------------------------------------------------------
# Design-stage shortcut: no login screen
#
# OAMS_DEV_AUTOLOGIN=<employee_code> signs in a session-less request as that
# member (1/true/yes/on means the bootstrap administrator). It is a *who*, never
# a *what*: permission checks, org-unit scoping, the form tokens and the audit
# trail are untouched, so the pages behave exactly as they will once the login
# comes back. It is deliberately unset by default, and the UI shows a badge while
# it is on, because a deployment that forgets to turn it off has no login at all.
# ---------------------------------------------------------------------------
DEV_AUTOLOGIN_ON = {"1", "true", "yes", "on"}
DEV_AUTOLOGIN_OFF = {"", "0", "false", "no", "off", "none"}
DEV_AUTOLOGIN_ADMIN_CODE = "ADMIN001"
# The switcher in the badge writes the chosen employee_code here rather than
# rewriting .env and restarting the server: a design review is exactly the moment
# you need to see the same page as a manager, then as somebody with no rights.
DEV_WORKER_COOKIE = "oams_dev_worker"
# Three levels and only three: the company administrator, the administrator of
# one or more departments, and everybody else. A level is read from the grants --
# what the pages will actually let somebody do -- never from a job title, because
# a title is a label somebody typed.
ROLE_SYSTEM = "系统管理员"
ROLE_DEPARTMENT = "部门管理员"
ROLE_STAFF = "普通员工"
ROLE_RANK = {ROLE_SYSTEM: 0, ROLE_DEPARTMENT: 1, ROLE_STAFF: 2}
# Company-wide administration is exactly the grant that opens the settings pages;
# anything narrower lifts somebody to department administrator and no higher.
COMPANY_ADMIN_GRANT = "workforce.grants:manage"
# Changing other people's work -- rather than only your own -- is what makes
# somebody a department administrator. Reviewing a page is not in this list: a
# reviewer still only reads their own department.
# Public on purpose: the collaboration directory groups the roster by exactly
# this rule ("who can change somebody else's work"), and a second copy of the
# list would drift from the one admin_level() answers with.
ADMIN_RESOURCES = ("workforce.work_items", "workforce.members", "workforce.interfaces",
                   "workforce.positions", "workforce.assignments", "workforce.org_units",
                   "workforce.grants")
_ADMIN_GRANT_KEYS = frozenset(f"{resource}:manage" for resource in ADMIN_RESOURCES)
# Kept under the old names for the callers that still say "DEV_ROLE".
DEV_ROLE_ADMIN = ROLE_SYSTEM
DEV_ROLE_MANAGER = ROLE_DEPARTMENT
DEV_ROLE_STAFF = ROLE_STAFF

_DEV_PRINCIPAL_SQL = """SELECT m.member_id, m.employee_code, m.display_name, m.employment_status, m.token_version,
          NULL::uuid AS session_id, m.token_version AS session_token_version,
          (SELECT p.title FROM oams.position_assignments a
             JOIN oams.positions p USING (position_id)
            WHERE a.member_id = m.member_id
              AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now()
              AND (a.ends_at IS NULL OR now() < a.ends_at)
            ORDER BY a.starts_at DESC LIMIT 1) AS position_title
   FROM oams.workforce_members m
  WHERE m.employee_code = %s AND m.employment_status = 'active'"""

_DEV_IDENTITY_SQL = """SELECT m.member_id, m.employee_code, m.display_name,
          (SELECT p.title FROM oams.position_assignments a
             JOIN oams.positions p USING (position_id)
            WHERE a.member_id = m.member_id
              AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now()
              AND (a.ends_at IS NULL OR now() < a.ends_at)
            ORDER BY a.starts_at DESC LIMIT 1) AS position_title,
          (SELECT coalesce(array_agg(DISTINCT g.resource || ':' || g.action), '{}')
             FROM oams.permission_grants g
            WHERE g.revoked_at IS NULL
              AND g.effect = 'allow'
              AND g.valid_from <= now()
              AND (g.valid_until IS NULL OR now() < g.valid_until)
              AND ((g.subject_type = 'member' AND g.member_id = m.member_id)
                OR (g.subject_type = 'position' AND EXISTS (
                     SELECT 1 FROM oams.position_assignments a
                      WHERE a.position_id = g.position_id
                        AND a.member_id = m.member_id
                        AND a.status IN ('active', 'scheduled'))))) AS grants
   FROM oams.workforce_members m
  WHERE m.employment_status = 'active'"""

_DEV_WARNED = False
_DEV_IDENTITIES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DEV_IDENTITIES_TTL = 15.0


def dev_role_label(grants: list[str] | tuple[str, ...] | None) -> str:
    """Name the level a member's grants actually add up to.

    The same rule admin_level() applies against the database, evaluated on a list
    of "resource:action" keys, so the badge and the pages can never disagree.
    """
    held = {str(grant) for grant in (grants or ())}
    if COMPANY_ADMIN_GRANT in held:
        return ROLE_SYSTEM
    if held & _ADMIN_GRANT_KEYS:
        return ROLE_DEPARTMENT
    return ROLE_STAFF


def dev_identities() -> list[dict[str, Any]]:
    """Every active member, strong enough to weak, labelled by their real grants.

    Cached for a few seconds: the badge asks on every render, and nobody changes
    another person's grants faster than that. An empty list when the shortcut is
    off, so a template cannot leak the roster into a login screen by accident.
    """
    if (settings.dev_autologin or "").strip().lower() in DEV_AUTOLOGIN_OFF:
        return []
    cached = _DEV_IDENTITIES.get(settings.database_url)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DEV_IDENTITIES_TTL:
        return cached[1]
    try:
        with connection() as conn:
            rows = conn.execute(_DEV_IDENTITY_SQL).fetchall()
    except Exception:  # pragma: no cover - a database that is not migrated yet
        return []
    people = [dict(row, role_label=dev_role_label(row["grants"])) for row in rows]
    people.sort(key=lambda person: (ROLE_RANK.get(person["role_label"], 9), person["employee_code"]))
    _DEV_IDENTITIES[settings.database_url] = (now, people)
    return people


def forget_dev_identities() -> None:
    """Drop the identity cache (tests, and a caller that just changed grants)."""
    _DEV_IDENTITIES.clear()


def dev_autologin_target(request: Request | None = None) -> str | None:
    """The employee_code to sign in as, or None when the shortcut is off.

    A browser that picked somebody in the badge wins over the environment default:
    the cookie is the switch's memory, and it only ever names a *who*.
    """
    raw = (settings.dev_autologin or "").strip()
    if raw.lower() in DEV_AUTOLOGIN_OFF:
        return None
    if request is not None:
        chosen = (request.cookies.get(DEV_WORKER_COOKIE) or "").strip()
        if chosen:
            return chosen
    if raw.lower() in DEV_AUTOLOGIN_ON:
        return DEV_AUTOLOGIN_ADMIN_CODE
    return raw


def dev_principal(request: Request | None = None) -> dict[str, Any] | None:
    """Resolve the shortcut member, or None when it is off or the code is unknown.

    An unknown code returns None on purpose: the request then lands on the login
    screen, which is confusing for exactly one second and much safer than
    pretending to be somebody.
    """
    global _DEV_WARNED
    target = dev_autologin_target(request)
    if target is None:
        return None
    with connection() as conn:
        row = conn.execute(_DEV_PRINCIPAL_SQL, (target,)).fetchone()
    if row is None:
        return None
    if not _DEV_WARNED:
        _DEV_WARNED = True
        print(f"[oams] DEV AUTOLOGIN IS ON: every session-less request is signed "
              f"in as {target}. Unset OAMS_DEV_AUTOLOGIN before any real deployment.",
              file=sys.stderr, flush=True)
    return dict(row, dev_autologin=True)


def current_principal(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        # A caller with no token at all gets the design-stage shortcut when it is
        # on, so curl and the dev shell do not have to mint one first. A token
        # that is present but wrong still fails below: a stale session has to
        # stay visible instead of being silently upgraded to the shortcut.
        shortcut = dev_principal()
        if shortcut is not None:
            return shortcut
        raise HTTPException(status_code=401, detail="Bearer session required", headers={"WWW-Authenticate": "Bearer"})
    row = principal_for_token(credentials.credentials)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    return row


def principal_from_request(request: Request) -> dict[str, Any] | None:
    """The browser variant: same session table, same resolution, cookie as carrier."""
    token = request.cookies.get(settings.web_session_cookie)
    row = principal_for_token(token) if token else None
    # No cookie, or one that no longer resolves: fall through to the shortcut
    # rather than bouncing to the login screen in the middle of a design review.
    return row if row is not None else dev_principal(request)


#: 授权能管到的范围有两档：整个公司（`all`）与指定部门（`org_unit` + 部门 id）。
#: 一档都不命中就是在这件事上没有权限——窄授权不是「等于没有」，是「只在这些部门里」。
_SUBJECT_MATCH = """((g.subject_type = 'member' AND g.member_id = %s)
               OR (g.subject_type = 'position' AND EXISTS (
                    SELECT 1 FROM oams.position_assignments a
                    WHERE a.position_id = g.position_id AND a.member_id = %s
                      AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                      AND (a.ends_at IS NULL OR now() < a.ends_at))))"""


def effective_grants(conn, member_id: Any, resource: str, action: str, *,
                     org_unit_id: Any = None, company_only: bool = False) -> list[dict[str, Any]]:
    """这一件事上，落在**这个人的范围里**的授权行。

    - 不传 `org_unit_id`：两档都算（全公司的 + 任何部门的），问的是「他有没有这个权」。
    - 传 `org_unit_id`：全公司的 + 正好指定这个部门的，问的是「他能不能在这个部门做」。
    - `company_only=True`：只认 `scope_type='all'`，「公司级」这类判断专用。

    范围过滤只在这里做：每个页面各写一套，迟早会有一套写漏。
    """
    sql = f"""SELECT g.effect, g.scope_type, g.scope_org_unit_id, g.scope_definition
              FROM oams.permission_grants g
              WHERE g.resource = %s AND g.action = %s AND g.revoked_at IS NULL
                AND g.valid_from <= now() AND (g.valid_until IS NULL OR now() < g.valid_until)
                AND {_SUBJECT_MATCH}"""
    params: list[Any] = [resource, action, member_id, member_id]
    if company_only:
        sql += " AND g.scope_type = 'all'"
    elif org_unit_id is not None:
        sql += (" AND (g.scope_type = 'all'"
                " OR (g.scope_type = 'org_unit' AND g.scope_org_unit_id = %s))")
        params.append(org_unit_id)
    return conn.execute(sql, params).fetchall()


def has_permission(conn, member_id: Any, resource: str, action: str, *,
                   org_unit_id: Any = None, company_only: bool = False) -> bool:
    grants = effective_grants(conn, member_id, resource, action,
                              org_unit_id=org_unit_id, company_only=company_only)
    if not grants:
        return False
    return not any(grant["effect"] == "deny" for grant in grants)


def active_org_unit_ids(conn, member_id: Any) -> set[Any]:
    """Departments reached through the member's currently effective positions."""
    rows = conn.execute(
        """SELECT DISTINCT p.org_unit_id FROM oams.position_assignments a
           JOIN oams.positions p USING (position_id)
           WHERE a.member_id = %s AND a.status IN ('active', 'scheduled')
             AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
             AND p.status = 'active'""",
        (member_id,),
    ).fetchall()
    return {row["org_unit_id"] for row in rows}


def has_company_scope(conn, member_id: Any) -> bool:
    """Company-wide operational visibility is reserved for security administrators."""
    return has_permission(conn, member_id, "workforce.grants", "manage", company_only=True)


def granted_org_unit_ids(conn, member_id: Any, resource: str | None = None,
                         action: str | None = None) -> set[Any]:
    """凭授权（`scope_type='org_unit'`）管得到的部门，用于「只授权几个部门」这一档。

    `resource`/`action` 给空就是「任何一件授权落到部门上都算」。deny 优先：
    同一个部门上既 allow 又 deny 的，不算管得到。
    """
    sql = f"""SELECT g.effect, g.scope_org_unit_id
              FROM oams.permission_grants g
              WHERE g.scope_type = 'org_unit' AND g.scope_org_unit_id IS NOT NULL
                AND g.revoked_at IS NULL
                AND g.valid_from <= now() AND (g.valid_until IS NULL OR now() < g.valid_until)
                AND {_SUBJECT_MATCH}"""
    params: list[Any] = [member_id, member_id]
    if resource is not None:
        sql += " AND g.resource = %s"
        params.append(resource)
    if action is not None:
        sql += " AND g.action = %s"
        params.append(action)
    allowed, denied = set(), set()
    for row in conn.execute(sql, params).fetchall():
        (denied if row["effect"] == "deny" else allowed).add(row["scope_org_unit_id"])
    return allowed - denied


def has_all_scope_grant(conn, member_id: Any, resource: str | None = None,
                        action: str | None = None) -> bool:
    """有没有一条全公司范围（`scope_type='all'`）的有效授权。

    `resource`/`action` 给了就只问这一件事——「任何一件落到全公司上都算数」会把
    「能看全公司报表」误当成「能进每个部门」。
    """
    sql = f"""SELECT 1 FROM oams.permission_grants g
              WHERE g.scope_type = 'all' AND g.effect = 'allow' AND g.revoked_at IS NULL
                AND g.valid_from <= now() AND (g.valid_until IS NULL OR now() < g.valid_until)
                AND {_SUBJECT_MATCH}"""
    params: list[Any] = [member_id, member_id]
    if resource is not None:
        sql += " AND g.resource = %s"
        params.append(resource)
    if action is not None:
        sql += " AND g.action = %s"
        params.append(action)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def all_department_ids(conn) -> set[Any]:
    """所有在用的部门（公司本级除外）——用来把「全公司范围」摊成部门 id。"""
    rows = conn.execute(
        """SELECT org_unit_id FROM oams.org_units
            WHERE status = 'active' AND unit_type <> 'company'""").fetchall()
    return {row["org_unit_id"] for row in rows}


def accessible_org_unit_ids(conn, member_id: Any, resource: str | None = None,
                            action: str | None = None) -> set[Any]:
    """这个人**能进**哪些部门：岗位所在的，加上被授权管的那几个。

    两条边界，都是踩过的：

    * `scope_type='all'` 只在**这个人没有岗位座位**时摊成全部在用部门。现有种子
      里 67 条授权全是 `all`（存量数据只有这一档），若把 `all` 一律当全公司，设计部
      负责人立刻能看到行政部的页——岗位所在就是他的范围。没有座位的人拿全公司
      范围，本来就是公司级角色，摊成全部部门才对。
    * `resource`/`action` 给了就按这件事问（`has_all_scope_grant` 同口径），不给
      就只认岗位与点名部门的授权——「任何一件授权」不能变成「能进这个部门」。
    """
    seats = active_org_unit_ids(conn, member_id)
    units = seats | granted_org_unit_ids(conn, member_id, resource, action)
    if not seats and has_all_scope_grant(conn, member_id, resource, action):
        units |= all_department_ids(conn)
    return units


def has_admin_rights(conn, member_id: Any) -> bool:
    """Can this member change other people's work, or only their own?"""
    return any(has_permission(conn, member_id, resource, "manage") for resource in ADMIN_RESOURCES)


def admin_level(conn, member_id: Any) -> str:
    """The one place that decides which of the three levels a member sits at.

    Company scope is the settings-page grant; anything else in the administrator
    set makes them the administrator of their own departments; everybody else is
    staff. Pages ask this rather than repeating the grant list in their SQL.
    """
    if has_company_scope(conn, member_id):
        return ROLE_SYSTEM
    if has_admin_rights(conn, member_id):
        return ROLE_DEPARTMENT
    return ROLE_STAFF


def administered_org_unit_ids(conn, member_id: Any) -> set[Any]:
    """The departments a member runs: their own departments, once they manage anything."""
    if not has_admin_rights(conn, member_id):
        return set()
    return accessible_org_unit_ids(conn, member_id)


def can_access_org_unit(conn, member_id: Any, org_unit_id: Any) -> bool:
    if has_company_scope(conn, member_id):
        return True
    return org_unit_id in accessible_org_unit_ids(conn, member_id)


def require_org_unit_access(conn, member_id: Any, org_unit_id: Any) -> None:
    if not can_access_org_unit(conn, member_id, org_unit_id):
        raise HTTPException(
            status_code=403,
            detail="这个部门不在你的范围内：你的岗位和被授权的部门都不含它")


def revoke_session(conn, session_id: Any, member_id: Any) -> None:
    """Logout and operator revocation share this, so both leave the same audit trail."""
    conn.execute("UPDATE oams.workforce_sessions SET revoked_at = now() WHERE session_id = %s", (session_id,))
    conn.execute(
        """INSERT INTO oams.workforce_audit_events
           (actor_member_id, event_type, subject_type, subject_id, reason)
           VALUES (%s, 'session.revoked', 'session', %s, '本人结束了当前登录会话')""",
        (member_id, session_id),
    )


def require_permission(resource: str, action: str):
    def check(principal: dict[str, Any] = Depends(current_principal)) -> dict[str, Any]:
        with connection() as conn:
            if not has_permission(conn, principal["member_id"], resource, action):
                raise HTTPException(status_code=403, detail="Permission denied")
        return principal
    return check
