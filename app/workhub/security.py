import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from workhub.config import settings
from workhub.db import connection

bearer = HTTPBearer(auto_error=False)


def digest_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_token() -> str:
    return secrets.token_urlsafe(40)


def issue_session(member_id: str, provider: str = "administrator-issued") -> tuple[str, datetime]:
    token = new_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    with connection() as conn:
        member = conn.execute(
            "SELECT employment_status, token_version FROM workhub.workforce_members WHERE member_id = %s FOR UPDATE",
            (member_id,),
        ).fetchone()
        if not member or member["employment_status"] != "active":
            raise ValueError("member must exist and be active")
        conn.execute(
            "INSERT INTO workhub.workforce_sessions (member_id, token_digest, token_version, expires_at, auth_provider) VALUES (%s, %s, %s, %s, %s)",
            (member_id, digest_token(token), member["token_version"], expires_at, provider),
        )
        conn.execute(
            "INSERT INTO workhub.workforce_audit_events (event_type, subject_type, subject_id, reason, metadata) VALUES ('session.issued', 'member', %s, %s, %s)",
            (member_id, "session issued through trusted operator CLI", Jsonb({"provider": provider})),
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
                      s.session_id, s.token_version AS session_token_version
               FROM workhub.workforce_sessions s
               JOIN workhub.workforce_members m USING (member_id)
               WHERE s.token_digest = %s AND s.revoked_at IS NULL
                 AND s.expires_at > now() AND m.employment_status = 'active'""",
            (digest_token(token),),
        ).fetchone()
    if not row or row["token_version"] != row["session_token_version"]:
        return None
    return row


def current_principal(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer session required", headers={"WWW-Authenticate": "Bearer"})
    row = principal_for_token(credentials.credentials)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    return row


def principal_from_request(request: Request) -> dict[str, Any] | None:
    """The browser variant: same session table, same resolution, cookie as carrier."""
    token = request.cookies.get(settings.web_session_cookie)
    if not token:
        return None
    return principal_for_token(token)


def effective_grants(conn, member_id: Any, resource: str, action: str) -> list[dict[str, Any]]:
    # SCOPE GAP: only scope_type='all' is honoured today, so a narrow grant gives
    # nothing and a broad grant gives everything. Row-level range filtering has to
    # land here -- not in each endpoint, and not in the UI -- before a second
    # department joins.
    return conn.execute(
        """SELECT g.effect, g.scope_type, g.scope_org_unit_id, g.scope_definition
           FROM workhub.permission_grants g
           WHERE g.resource = %s AND g.action = %s AND g.revoked_at IS NULL
             AND g.valid_from <= now() AND (g.valid_until IS NULL OR now() < g.valid_until)
             AND g.scope_type = 'all'
             AND ((g.subject_type = 'member' AND g.member_id = %s)
               OR (g.subject_type = 'position' AND EXISTS (
                    SELECT 1 FROM workhub.position_assignments a
                    WHERE a.position_id = g.position_id AND a.member_id = %s
                      AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                      AND (a.ends_at IS NULL OR now() < a.ends_at))))""",
        (resource, action, member_id, member_id),
    ).fetchall()


def has_permission(conn, member_id: Any, resource: str, action: str) -> bool:
    grants = effective_grants(conn, member_id, resource, action)
    if not grants:
        return False
    return not any(grant["effect"] == "deny" for grant in grants)


def active_org_unit_ids(conn, member_id: Any) -> set[Any]:
    """Departments reached through the member's currently effective positions."""
    rows = conn.execute(
        """SELECT DISTINCT p.org_unit_id FROM workhub.position_assignments a
           JOIN workhub.positions p USING (position_id)
           WHERE a.member_id = %s AND a.status IN ('active', 'scheduled')
             AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
             AND p.status = 'active'""",
        (member_id,),
    ).fetchall()
    return {row["org_unit_id"] for row in rows}


def has_company_scope(conn, member_id: Any) -> bool:
    """Company-wide operational visibility is reserved for security administrators."""
    return has_permission(conn, member_id, "workforce.grants", "manage")


def can_access_org_unit(conn, member_id: Any, org_unit_id: Any) -> bool:
    return has_company_scope(conn, member_id) or org_unit_id in active_org_unit_ids(conn, member_id)


def require_org_unit_access(conn, member_id: Any, org_unit_id: Any) -> None:
    if not can_access_org_unit(conn, member_id, org_unit_id):
        raise HTTPException(status_code=403, detail="org unit is outside the member assignment scope")


def revoke_session(conn, session_id: Any, member_id: Any) -> None:
    """Logout and operator revocation share this, so both leave the same audit trail."""
    conn.execute("UPDATE workhub.workforce_sessions SET revoked_at = now() WHERE session_id = %s", (session_id,))
    conn.execute(
        """INSERT INTO workhub.workforce_audit_events
           (actor_member_id, event_type, subject_type, subject_id, reason)
           VALUES (%s, 'session.revoked', 'session', %s, 'member ended the current session')""",
        (member_id, session_id),
    )


def require_permission(resource: str, action: str):
    def check(principal: dict[str, Any] = Depends(current_principal)) -> dict[str, Any]:
        with connection() as conn:
            if not has_permission(conn, principal["member_id"], resource, action):
                raise HTTPException(status_code=403, detail="Permission denied")
        return principal
    return check
