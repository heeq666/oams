import hashlib
import ipaddress
import json
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb

from oams.config import settings
from oams.db import connection
from oams.sensitive_fields import registry_rows, sensitive_keys_in

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
_DEV_REFUSED_WARNED = False
_DEV_IDENTITIES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DEV_IDENTITIES_TTL = 15.0


def _loopback_address(value: Any) -> bool | None:
    """True/False for an IP literal, None when the value is not one.

    A hostname is deliberately *not* resolved here: resolution needs the network,
    it can disagree with what the socket actually bound to, and a name that fails
    to resolve would turn a safety check into a crash.
    """
    text = str(value or "").strip().strip("[]")
    if not text:
        return None
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return None


def dev_autologin_refusal(request: Request | None = None) -> str | None:
    """Why the shortcut must not sign anybody in here, or None when it may.

    The shortcut has no login at all, so it is only ever safe on an instance that
    nothing but this machine can reach. Two ways of learning that it is not:

    * the configured bind address names an address other machines can reach
      (`0.0.0.0`, `::`, a LAN address, a real hostname) -- a setting, so this
      fires even before a request arrives;
    * a live request arrived from a non-loopback client -- the binding might have
      been passed on the command line instead of the settings (`uvicorn --host`),
      and then this is the only thing that sees the truth.

    Only positive evidence refuses: an address we cannot read as an IP (the
    in-process test client calls itself `testserver`) keeps working, because the
    startup gate below is what actually stops a real deployment.

    Asking this question is itself only meaningful while the shortcut is on, so a
    switched-off shortcut is never refused -- the rule is about "no login out in
    the open", not about which port anything listens on.
    """
    if (settings.dev_autologin or "").strip().lower() in DEV_AUTOLOGIN_OFF:
        return None
    bind = (settings.bind_host or "").strip()
    binding_reads_local = _loopback_address(bind)
    if binding_reads_local is False:
        return f"监听地址是 {bind}（别的机器也能连），免登录会让谁打开都是管理员"
    if binding_reads_local is None and bind and bind.lower() not in {"localhost", "localhost.localdomain"}:
        return f"监听地址是 {bind}（不是回环地址，别的机器可能连得到）"
    if request is None:
        return None
    client = request.scope.get("client")
    host = client[0] if isinstance(client, (tuple, list)) and client else None
    if _loopback_address(host) is False:
        return f"这次请求来自 {host}（不是本机），免登录只对本机开放"
    return None


def _warn_refused(reason: str) -> None:
    """Say it once, on stderr, so an operator sees the shortcut did not take."""
    global _DEV_REFUSED_WARNED
    if _DEV_REFUSED_WARNED:
        return
    _DEV_REFUSED_WARNED = True
    print(f"[oams] DEV AUTOLOGIN REFUSED: {reason}。"
          f"把 OAMS_DEV_AUTOLOGIN 去掉，或改绑 127.0.0.1。", file=sys.stderr, flush=True)


def assert_dev_autologin_is_local() -> None:
    """Refuse to start: the shortcut on an address other machines reach.

    Called by `oams serve` before uvicorn binds. Starting anyway would print a
    warning nobody reads and then hand out the administrator session to the
    network; failing to boot is the only answer that cannot be ignored.
    """
    if (settings.dev_autologin or "").strip().lower() in DEV_AUTOLOGIN_OFF:
        return
    reason = dev_autologin_refusal()
    if reason is not None:
        raise SystemExit(
            f"拒绝启动：{reason}。\n"
            f"免登录（OAMS_DEV_AUTOLOGIN）没有登录这一步，只许绑在 127.0.0.1 上：\n"
            f"  OAMS_DEV_AUTOLOGIN=0 或去掉它 → 要开放给别人访问就恢复登录；\n"
            f"  OAMS_BIND_HOST=127.0.0.1 → 只在本机试用。")


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
    if dev_autologin_refusal() is not None:
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

    The shortcut is also off whenever the server is reachable from another machine
    (see dev_autologin_refusal): it has no login at all, so it belongs to a
    loopback-only instance and nowhere else.
    """
    raw = (settings.dev_autologin or "").strip()
    if raw.lower() in DEV_AUTOLOGIN_OFF:
        return None
    refusal = dev_autologin_refusal(request)
    if refusal is not None:
        _warn_refused(refusal)
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


#: 读人事敏感字段的权限。**不随「管理层」自动获得**：管理员是运维角色，技术权限不该
#: 自动换成业务字段的读权（判据在 `sensitive_fields` 模块开头，六个字段全部要单独授权）。
SENSITIVE_FIELD_RESOURCE = "employee.sensitive_field"


def can_read_sensitive_field(conn, member_id: Any, field_key: str) -> bool:
    """这一格敏感字段他能不能看明文。

    判据两层，缺一不可：

    1. 这一格**登记过**——没登记的敏感键一律不可读。不是「无所谓」，是「没人判过」；
    2. 登记行说要单独授权（`requires_explicit_grant`）时，他得有
       `employee.sensitive_field:read` 的授权。登记行说不必单独授权（这家公司不把它
       当敏感），就按登记表放行——口径跟着库走，不跟着代码里的出厂设置走。
    """
    row = registry_rows(conn).get(field_key)
    if row is None:
        return False
    if not row["requires_explicit_grant"]:
        return True
    return has_permission(conn, member_id, SENSITIVE_FIELD_RESOURCE, "read")


#: 读人事敏感字段明文的事件类型。事后要查「谁在什么时候看了谁的敏感字段」就按它捞。
SENSITIVE_READ_EVENT = "sensitive_field.read"


def readable_sensitive_fields(conn, member_id: Any, subject_member_id: Any, columns,
                              *, via: str = "api") -> frozenset[str]:
    """这些列里，他能看明文的那些敏感键；**真解锁了就记一条审计**。

    两个成员别搞混：`member_id` 是**看的人**（审计的 actor），`subject_member_id` 是
    **被看的人**（审计的 subject_id）。要求调用方显式给出被看的人，是为了不给
    「忘了记审计」留一条静默的路。

    口径（2026-11-09 定，别自己扩）：

    1. 只在**真解锁**时写——打码的不写。翻一遍名单几百行，把打码也记进去，真出事那条
       就淹在噪声里了；
    2. 一次调用一条，`field_keys` 排好序放进 `metadata`——不按字段逐条写；
    3. 明文值一个字都不进审计：`metadata` 里只有字段键与来源（`web` / `api`）。

    没有敏感列时**不查库也不写审计**（今天的人员表就是这种情况）——闸门还在，
    只是没有需要打码的字段，别为了「看起来在跑」多打一次库。
    """
    keys = sensitive_keys_in(columns)
    if not keys:
        return frozenset()
    readable = frozenset(key for key in keys if can_read_sensitive_field(conn, member_id, key))
    if not readable:
        return frozenset()
    # 审计写在这里而不是调 `api.audit`：`api` 反过来 import 本模块，调它就成了循环。
    conn.execute(
        """INSERT INTO oams.workforce_audit_events
           (actor_member_id, event_type, subject_type, subject_id, reason, metadata)
           VALUES (%s, %s, 'member', %s, %s, %s)""",
        (member_id, SENSITIVE_READ_EVENT, subject_member_id, "读取人事敏感字段明文",
         Jsonb({"field_keys": sorted(readable), "via": via},
               dumps=lambda obj: json.dumps(obj, default=str))),
    )
    return readable


def shared_org_unit_ids(conn, member_id: Any) -> set[Any]:
    """哪些部门**有页面开给了我**——左侧栏把部门列出来时要用。

    与 `can_view_interface` 同源：同一条岗位祖先链、同一张
    `oams.interface_access`，读或写任一为真就算（公司根那一行也算，那是「全公司可见」）。
    只认 `active` 的页：草稿、待审页不是「开给你的东西」，列进导航等于让人白点一次。

    注意这不是「我能进这个部门」——那个判据是 `can_access_org_unit`。这里回答的是
    「那里有我的东西可看」，两件事分开，别拿它去放行建页或签审。
    """
    seats = active_org_unit_ids(conn, member_id)
    if not seats:
        return set()
    rows = conn.execute(
        _audience_chain_cte() + """
        SELECT DISTINCT i.org_unit_id
          FROM oams.interfaces i
          JOIN oams.interface_access a
            ON a.owner_org_unit_id = i.org_unit_id AND a.slug = i.slug
         WHERE i.status = 'active' AND (a.can_read OR a.can_write)
           AND a.org_unit_id IN (SELECT org_unit_id FROM chain)""",
        (list(seats),),
    ).fetchall()
    return {row["org_unit_id"] for row in rows}


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


def company_seat(conn, member_id: Any) -> bool:
    """这个人是不是坐在**公司本级**的岗位上。

    「全公司范围」只有这一种来源：他的岗位挂在一个 `unit_type='company'` 的部门下。
    不能拿 `scope_type='all'` 当全公司范围——现有种子 67 条授权**全部**是 `all` 档
    （`scripts/load_department_data.py` 那句注释：除非 spec 另说，每条授权都写 all），
    在这套数据里 `all` 被当成「这个岗位有这件事的权」的标记，不是「范围到全公司」。
    早先按「有没有 `all` 授权」判，真库里每个部门负责人都成了全公司读者。

    判据与 `active_org_unit_ids` 同源（同一个时限、同一批状态），不另算一套。
    """
    row = conn.execute(
        """SELECT 1 FROM oams.position_assignments a
             JOIN oams.positions p USING (position_id)
             JOIN oams.org_units o USING (org_unit_id)
            WHERE a.member_id = %s
              AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
              AND o.unit_type = 'company'
            LIMIT 1""",
        (member_id,),
    ).fetchone()
    return row is not None


def readable_org_unit_ids(conn, member_id: Any, resource: str, action: str) -> set[Any] | None:
    """读这件事时，这个人该被收窄到哪些部门；`None` 表示**不收窄**（全公司读者）。

    返回 `None` 只有一种情形：他坐在公司本级的岗位上（`company_seat`）。这时把全公司
    给他——系统管理员、公司本级的岗位本来就该看全公司。

    其余情形返回「岗位所在的部门 ∪ 这件事上点名授权给他的部门」。两者都没有就是一个
    空集合：他确实有读权，但范围内没有任何部门，端点返回空列表而不是报错。

    **`scope_type='all'` 不能当全公司范围**：全套种子数据里每条授权都是 `all` 档，
    它在这里是「这个岗位有这件事的权」的标记。我第一版按「有没有 `all` 授权」判，
    在 55433 上跑出来 GA001/FIN001/IT001 全部 readable=None（= 每个部门负责人都能看
    全公司），而用例全绿——因为用例自己造的授权写的是 `org_unit` 档。真实数据里
    一条那样的授权都没有。判据只能落在「座位挂在哪一级部门」上。

    这是**唯一**决定「读的范围」的地方。端点只拿它的返回值去 `= ANY(%s)`，不自己
    再写一套——每个页面各写一套，迟早有一套写漏。

    范围本身仍交给 `accessible_org_unit_ids`（岗位 ∪ 定点授权），这里只多回答一件事：
    有没有公司本级的座位。
    """
    if company_seat(conn, member_id):
        return None
    return accessible_org_unit_ids(conn, member_id, resource, action)


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


# ---------------------------------------------------------------------------
# 页面权限（谁能看 / 谁能填）：页面自己的属性，落在 oams.interface_access
#
# 契约见 docs/architecture/interface-access-contract.md。三条不能破的：
#   1. 只放宽不收紧——本部门的人先过 `can_access_org_unit` 这一条，没行也不会少权限；
#   2. 权限挂在 `(owner_org_unit_id, slug)` 上，不挂 `interface_id`（版本行每发布
#      一次就换一个，挂上去等于改个标题权限就清零）；
#   3. 祖先链用一条递归 CTE 走完，不在 Python 里循环查库——页面打开是热路径。
# ---------------------------------------------------------------------------


def _audience_uuid(value: Any) -> Any:
    """部门 id 可能是 UUID（库里读出来的），也可能是字符串（表单、JSON 路径参数）。

    集合里存的是 UUID，`str == UUID` 恒假——不掰正，传字符串的调用方会静默得到
    「没有权限」，而不是报错。掰不动就原样返回，让库去报真正的错。
    """
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return value


def _interface_exists(conn, owner_org_unit_id: Any, slug: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM oams.interfaces WHERE org_unit_id = %s AND slug = %s LIMIT 1",
        (owner_org_unit_id, slug),
    ).fetchone() is not None


def _audience_chain_cte() -> str:
    """给「我这个部门的祖先链」用的递归 CTE，起点是传入的部门 id 数组。"""
    return """WITH RECURSIVE chain AS (
                  SELECT org_unit_id FROM oams.org_units
                   WHERE org_unit_id = ANY(%s::uuid[])
                  UNION
                  SELECT parent.parent_org_unit_id
                    FROM oams.org_units parent
                    JOIN chain c ON parent.org_unit_id = c.org_unit_id
                   WHERE parent.parent_org_unit_id IS NOT NULL)"""


def _seat_chain(conn, member_id: Any) -> set[Any]:
    """岗位所在部门 + 它们的所有祖先（走到公司根）——一次查询，不在 Python 里爬树。"""
    seats = active_org_unit_ids(conn, member_id)
    if not seats:
        return set()
    rows = conn.execute(
        _audience_chain_cte() + " SELECT org_unit_id FROM chain",
        (list(seats),),
    ).fetchall()
    return {row["org_unit_id"] for row in rows}


def interface_audience(conn, owner_org_unit_id: Any, slug: str) -> list[dict[str, Any]]:
    """这一页开给了哪些部门（本部门不入表，它本来就永远能看能填）。"""
    owner_org_unit_id = _audience_uuid(owner_org_unit_id)
    rows = conn.execute(
        """SELECT a.org_unit_id, u.org_code, u.name, u.unit_type,
                  a.can_read, a.can_write, a.can_base, a.granted_at
             FROM oams.interface_access a
             JOIN oams.org_units u USING (org_unit_id)
            WHERE a.owner_org_unit_id = %s AND a.slug = %s
            ORDER BY u.org_code""",
        (owner_org_unit_id, slug),
    ).fetchall()
    return [
        {
            "org_unit_id": row["org_unit_id"],
            "org_code": row["org_code"],
            "name": row["name"],
            "unit_type": row["unit_type"],
            "can_read": row["can_read"],
            "can_write": row["can_write"],
            "can_base": row["can_base"],
            "granted_at": row["granted_at"],
        }
        for row in rows
    ]


def reference_pages_for(conn, member_id: Any) -> list[dict[str, Any]]:
    """别人开给我的页：哪几页我可以照着做一版，哪几页我连条目也能看。

    「可作参考」这件事必须在**取定义之前**就能被发现——否则一个外部 AI 手里有票、
    有工具，却不知道有哪一页可以照着做，这条路就只有人来告诉它页面编号。所以这里
    按「我坐的部门（含祖先）」把开给我的行拉出来，和 `interface_rights` 用同一条座位链
    算，不另写一套判据。

    自己部门的页不算「别人开给我的」：本部门页面本来就能看能填，`interface_access`
    也不收录它（见 `interface_audience`）；那条规矩在这里不复述一遍判据。
    """
    chain = _seat_chain(conn, member_id)
    if not chain:
        return []
    rows = conn.execute(
        """SELECT i.interface_id, i.slug, i.name, i.org_unit_id,
                  u.org_code, u.name AS org_name,
                  a.can_read, a.can_write, a.can_base
             FROM oams.interface_access a
             JOIN oams.interfaces i
               ON i.org_unit_id = a.owner_org_unit_id
              AND i.slug = a.slug
              AND i.status = 'active'
             JOIN oams.org_units u ON u.org_unit_id = a.owner_org_unit_id
            WHERE a.org_unit_id = ANY(%s)
              AND (a.can_base OR a.can_read)
            ORDER BY u.org_code, i.slug""",
        (list(chain),),
    ).fetchall()
    return [
        {
            "interface_id": str(row["interface_id"]),
            "slug": row["slug"],
            "name": row["name"],
            "from_org_unit_id": str(row["org_unit_id"]),
            "from_org_code": row["org_code"],
            "from_org_name": row["org_name"],
            "can_read": bool(row["can_read"]),
            "can_write": bool(row["can_write"]),
            "can_base": bool(row["can_base"]),
        }
        for row in rows
    ]


def _reachable_by_audience(conn, member_id: Any, owner_org_unit_id: Any, slug: str,
                           column: str) -> bool:
    """祖先链上有没有一行把这一页开给了这个人所在的部门。

    `column` 只由本模块的两个判据传 `can_read` / `can_write`，不是外部输入。
    """
    chain = _seat_chain(conn, member_id)
    if not chain:
        return False
    return conn.execute(
        f"""SELECT 1 FROM oams.interface_access a
             WHERE a.owner_org_unit_id = %s AND a.slug = %s AND a.{column}
               AND a.org_unit_id = ANY(%s::uuid[])
             LIMIT 1""",
        (owner_org_unit_id, slug, list(chain)),
    ).fetchone() is not None


def interface_access_via(conn, member_id: Any, owner_org_unit_id: Any, slug: str) -> dict[str, Any] | None:
    """这一页是靠哪一行开给这个人的——页头那句「谁开的」用它，不改判据。

    能不能看/能不能填仍由 :func:`can_view_interface` / :func:`can_fill_interface` 算
    （它们是「任一行够用就算」，公司根与点名部门可以同时命中）；这里只回答「该说哪
    一行」，口径与那两个函数刻意对齐：同一份 :func:`interface_audience`、同一条岗位
    祖先链。挑法是最具体的那行——本人岗位所在的部门 > 别的点名部门 > 公司根；一个都
    没命中就返回 None（说明不是靠授权进来的）。
    """
    seats = active_org_unit_ids(conn, member_id)
    if not seats:
        return None
    chain = _seat_chain(conn, member_id)
    rows = [row for row in interface_audience(conn, owner_org_unit_id, slug)
            if row["org_unit_id"] in chain]
    if not rows:
        return None
    for picked in (
        [row for row in rows if row["unit_type"] != "company" and row["org_unit_id"] in seats],
        [row for row in rows if row["unit_type"] != "company"],
        rows,
    ):
        if picked:
            return picked[0]
    return None


def interface_rights(conn, member_id: Any, owner_org_unit_id: Any, slug: str) -> dict[str, bool] | None:
    """这一页这个人三格各拿到没有：查看 / 编辑 / 可作参考。页不存在返回 None。

    三个 ``can_*_interface`` 判据都从这里取自己那一格，界面也取同一份结果——三处各算
    一遍是两套判据分叉的老路子，迟早显示的和真正放行的那一条对不上。

    部门内（含公司级、含被授权代管）三格全真：这是「只放宽不收紧」的保证。部门外
    只认页面自己开的范围，一行都没有就是三格全假。「能参考但看不到」是合法组合，
    所以三格分开算，不许用 can_read 兜底。
    """
    owner_org_unit_id = _audience_uuid(owner_org_unit_id)
    if not _interface_exists(conn, owner_org_unit_id, slug):
        return None
    if can_access_org_unit(conn, member_id, owner_org_unit_id):
        return {"can_read": True, "can_write": True, "can_base": True}
    chain = _seat_chain(conn, member_id)
    rows = ([row for row in interface_audience(conn, owner_org_unit_id, slug)
             if row["org_unit_id"] in chain] if chain else [])
    return {
        "can_read": any(row["can_read"] for row in rows),
        "can_write": any(row["can_write"] for row in rows),
        "can_base": any(row["can_base"] for row in rows),
    }


def can_view_interface(conn, member_id: Any, owner_org_unit_id: Any, slug: str) -> bool:
    """这一页这个人能不能打开。判据见 :func:`interface_rights`（「查看」那一格）。"""
    rights = interface_rights(conn, member_id, owner_org_unit_id, slug)
    return bool(rights and rights["can_read"])


def can_fill_interface(conn, member_id: Any, owner_org_unit_id: Any, slug: str) -> bool:
    """这一页这个人能不能提条目。判据见 :func:`interface_rights`（「编辑」那一格）。"""
    rights = interface_rights(conn, member_id, owner_org_unit_id, slug)
    return bool(rights and rights["can_write"])


def can_base_interface(conn, member_id: Any, owner_org_unit_id: Any, slug: str) -> bool:
    """这一页这个人能不能拿它当模板另建一版（权限弹窗第三格「可作参考」）。

    **绝不能让它顺手变成「能看」**：参考的是页面定义（字段、模块、版式），不是条目
    里的内容。给一个部门参考权而没给查看权，是合法组合——他要照着做一页自己的，
    而不是来读别人的数据。判据见 :func:`interface_rights`（「可作参考」那一格）。
    """
    rights = interface_rights(conn, member_id, owner_org_unit_id, slug)
    return bool(rights and rights["can_base"])


def set_interface_audience(conn, owner_org_unit_id: Any, slug: str, rows: list[dict[str, Any]], *,
                           granted_by: Any, note: str | None = None) -> None:
    """整体覆盖这一页的范围：先删本页所有行，再插「至少能看」的那些。

    * 同一部门写两次是覆盖不是追加（主键三段管着）；
    * `can_write` 必然蕴含 `can_read`——表上有 CHECK，这里先把它掰正，免得把约束
      当成调用方要读的错误；
    * 两格都假的行不入库（「一行都没有」就是「这个部门看不到」）。

    「谁能改这一页」「哪些部门允许授」由调用方（页面设置）按人复核，这里只落库。
    """
    owner_org_unit_id = _audience_uuid(owner_org_unit_id)
    conn.execute(
        "DELETE FROM oams.interface_access WHERE owner_org_unit_id = %s AND slug = %s",
        (owner_org_unit_id, slug),
    )
    wanted: dict[Any, tuple[bool, bool, bool]] = {}
    for row in rows:
        org_unit_id = _audience_uuid(row["org_unit_id"])
        can_write = bool(row.get("can_write"))
        can_read = bool(row.get("can_read")) or can_write
        can_base = bool(row.get("can_base"))
        # 三格全假的行不入库（「一行都没有」就是「这个部门什么权利都没有」）。
        # 「能参考但看不到」是合法组合，所以这一条不能只看 can_read。
        if not (can_read or can_base):
            continue
        wanted[org_unit_id] = (can_read, can_write, can_base)
    for org_unit_id, (can_read, can_write, can_base) in sorted(wanted.items(), key=lambda item: str(item[0])):
        conn.execute(
            """INSERT INTO oams.interface_access
                   (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base,
                    granted_by, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base,
             granted_by, note),
        )


def grantable_org_unit_ids(conn, member_id: Any) -> set[Any] | None:
    """这个人改页面权限时能把范围开给哪些部门。

    `None` = 公司级：任何在用部门（含公司根，那就是界面上的「全公司」）都能开。
    `set`  = 只有他座位所在的那几个在用的部门；**不含**公司根——「开给全公司」
          是公司级才有的动作，部门管理员给自己几个部门开范围就够了。
    """
    if has_company_scope(conn, member_id):
        return None
    seats = active_org_unit_ids(conn, member_id)
    if not seats:
        return set()
    rows = conn.execute(
        """SELECT org_unit_id FROM oams.org_units
            WHERE status = 'active' AND unit_type <> 'company'
              AND org_unit_id = ANY(%s::uuid[])""",
        (list(seats),),
    ).fetchall()
    return {row["org_unit_id"] for row in rows}


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
