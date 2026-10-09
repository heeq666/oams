import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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
            "INSERT INTO workhub.workforce_audit_events (event_type, subject_type, subject_id, reason, metadata) VALUES ('session.issued', 'member', %s, %s, jsonb_build_object('provider', %s))",
            (member_id, "session issued through trusted operator CLI", provider),
        )
    return token, expires_at


def current_principal(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer session required", headers={"WWW-Authenticate": "Bearer"})
    with connection() as conn:
        row = conn.execute(
            """SELECT m.member_id, m.display_name, m.employment_status, m.token_version,
                      s.session_id, s.token_version AS session_token_version
               FROM workhub.workforce_sessions s
               JOIN workhub.workforce_members m USING (member_id)
               WHERE s.token_digest = %s AND s.revoked_at IS NULL
                 AND s.expires_at > now() AND m.employment_status = 'active'""",
            (digest_token(credentials.credentials),),
        ).fetchone()
    if not row or row["token_version"] != row["session_token_version"]:
        raise HTTPException(status_code=401, detail="Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    return row


def require_permission(resource: str, action: str):
    def check(principal: dict[str, Any] = Depends(current_principal)) -> dict[str, Any]:
        with connection() as conn:
            grants = conn.execute(
                """SELECT g.effect, g.scope_type, g.scope_org_unit_id, g.scope_definition,
                          p.position_id AS granted_position_id
                   FROM workhub.permission_grants g
                   LEFT JOIN workhub.positions p ON p.position_id = g.position_id
                   WHERE g.resource = %s AND g.action = %s AND g.revoked_at IS NULL
                     AND g.valid_from <= now() AND (g.valid_until IS NULL OR now() < g.valid_until)
                     AND g.scope_type = 'all'
                     AND ((g.subject_type = 'member' AND g.member_id = %s)
                       OR (g.subject_type = 'position' AND EXISTS (
                            SELECT 1 FROM workhub.position_assignments a
                            WHERE a.position_id = g.position_id AND a.member_id = %s
                              AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                              AND (a.ends_at IS NULL OR now() < a.ends_at))))""",
                (resource, action, principal["member_id"], principal["member_id"]),
            ).fetchall()
            if not grants:
                raise HTTPException(status_code=403, detail="Permission denied")
            if any(g["effect"] == "deny" for g in grants):
                raise HTTPException(status_code=403, detail="Permission denied")
        return principal
    return check
