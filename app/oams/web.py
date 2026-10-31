"""Server-rendered board and minimal admin pages.

Why server rendering and not a single-page app: the target machine is a Windows
box with Python and PostgreSQL on it and nothing else. A Node build step would be
one more thing to break at deploy time, for four users and one board. The pages
are thin; replacing them later costs nothing downstream.

Two rules this file has to obey:

1. Authentication and authorisation are the same objects the JSON API uses. The
   cookie carries an ordinary session token, resolved by the same session table
   and checked against the same grants as a bearer token. Nothing here is a
   second way in -- if the UI did its own "is this person allowed" check, the
   pages would become a hole straight through require_permission.

2. The board renders from api.build_board, the same function behind
   /v1/board/org-units/{id}. What the page shows cannot drift from what the API
   returns, because it is literally the same data.

Deliberately absent: any form for a member to file their own progress. The first
version exists to test whether an AI can make reporting cheap; a convenient web
form would make people type it by hand and the measurement would be worthless.
Progress arrives through MCP.
"""
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from oams import admin_labels, api, collab, metrics, review as review_rules
from oams.config import settings
from oams.db import connection
from oams.descriptor import recorded_approval_gaps, validate_descriptor
from oams.render import RenderError, render_interface, render_read_only
from oams.schemas import (AssignmentCreate, GrantCreate, MemberCreate, OrgUnitCreate, PositionCreate,
                             ReportCreate, WorkItemCreate)
from oams.security import (DEV_WORKER_COOKIE, ROLE_DEPARTMENT, ROLE_STAFF, ROLE_SYSTEM,
                              active_org_unit_ids, admin_level, administered_org_unit_ids,
                              dev_autologin_target, dev_identities, dev_principal, has_company_scope,
                              has_permission, issue_session, principal_from_request,
                              principal_for_token, require_org_unit_access, revoke_session)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


def _display_time(value: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TZ).strftime(fmt)


def _badge_count(value: Any) -> str:
    """未读徽章上的字数：到 99 为止都写真实数字，再往上写 99+。

    徽章固定 16 高、宽度靠 HUG 撑，三位数会撑到 28 宽，一串长数字还会把
    左侧的会话名挤窄；真实条数另有去处（aria-label 与面板头「共 N 条未读」），
    徽章只负责「有多少没看」这一眼。
    """
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    return str(count) if count < 100 else "99+"


TEMPLATES.env.filters["dt"] = _display_time
TEMPLATES.env.filters["badge"] = _badge_count
# The global stays for anything that still wants the raw predicate; the page itself
# renders board.visible_cards, which api already filtered with the same function.
TEMPLATES.env.globals["in_layer"] = api.layer_matches
# The badge in the shell reads this per render, so a test can flip the setting
# without reloading the module.
TEMPLATES.env.globals["dev_mode"] = lambda: dev_autologin_target() is not None
# Who the badge may switch to, and the cookie it remembers the choice in. Both are
# empty/no-op while the shortcut is off, so the login screen never leaks a roster.
TEMPLATES.env.globals["dev_identities"] = dev_identities
TEMPLATES.env.globals["dev_cookie_name"] = DEV_WORKER_COOKIE

# 静态资源的版本串。
#
# 没有它的时候，浏览器会按自己的心情缓存 css：改完版的人刷新一次可能还是旧样式，
# 而「改完了吗」这种问题不该靠按 Ctrl+Shift+R 才能回答。版本串取文件内容的哈希，
# 不是手写的常量——常量一定会忘，哈希不会：改一个字节换一个 URL，浏览器自然回源。
#
# 静态目录也在这里定下来，api.py 挂载与这里查哈希必须看同一个目录，
# 否则 URL 上的版本串算的是另一份文件。
STATIC_DIR = Path(__file__).resolve().parent / "static"


@lru_cache(maxsize=512)
def _static_digest(path: str, mtime_ns: int, size: int) -> str:
    """按「绝对路径 + mtime + 大小」缓存的内容哈希。

    三个都进 key：文件被替换而大小不变时 mtime 会变，反过来 mtime 被 touch
    而内容不变只是多算一次，不影响正确性。
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8]


def static_version(relative: str) -> str:
    """这个静态文件当前的版本串；文件不在就返回空串。

    空串的意思是「说不出版本」，调用方据此退化成不缓存，而不是编一个假的。
    """
    relative = relative.lstrip("/")
    path = STATIC_DIR / relative
    try:
        stat = path.stat()
    except OSError:
        return ""
    return _static_digest(str(path), stat.st_mtime_ns, stat.st_size)


def static_url(relative: str) -> str:
    """模板里用：`{{ static_url('oams-modules.css') }}`。"""
    relative = relative.lstrip("/")
    version = static_version(relative)
    return f"/static/{relative}?v={version}" if version else f"/static/{relative}"

TEMPLATES.env.globals["static_url"] = static_url


STATUS_LABELS = {"planned": "未开始", "in_progress": "进行中", "blocked": "阻塞",
                 "done": "已完成", "cancelled": "已取消"}
KIND_LABELS = {"project": "项目", "routine": "常规", "adhoc": "临时"}
CHANNEL_LABELS = {"human": "人工", "agent": "AI 代笔"}

# The three states the concept doc requires to be visible on every entry (ch.6):
# 已进汇总 / 未关联 / 草稿. The SQL that decides them lives in api.ROLLUP_STATE_SQL,
# so the badge, the board panel and the JSON endpoint cannot disagree.
ROLLUP_STATE_LABELS = {
    "rolled": "已进汇总",
    "unlinked": "未关联，不进汇总",
    "draft": "草稿，不进汇总",
    "retracted": "已撤回",
}
# Badge classes that already exist in base.html; no new stylesheet rule needed.
ROLLUP_STATE_BADGES = {"rolled": "human", "unlinked": "vacant", "draft": "decision",
                       "retracted": ""}
ENTRY_LINK_PURPOSE = "oams:entry-link:v1"


def _with_rollup_labels(rows):
    """Give each entry the short Chinese label and badge class its state deserves."""
    rows = list(rows or [])
    marked = []
    for row in rows:
        item = dict(row)
        state = item.get("rollup_state") or "unlinked"
        item["rollup_state"] = state
        item["rollup_label"] = ROLLUP_STATE_LABELS.get(state, state)
        item["rollup_badge"] = ROLLUP_STATE_BADGES.get(state, "")
        item["rollup_blocker"] = _rollup_gap(state)
        marked.append(item)
    return marked


def _rollup_gap(state):
    """"Why is this one not in the company number", in the department's words.

    The concept doc asks the UI to name the missing condition instead of only
    marking the entry, so an unlinked entry says what would change its mind.
    """
    return {
        "unlinked": "还没关联工作项：关联后这条才会进公司汇总",
        "draft": "这条是试填数据：页面通过审核后才进公司汇总",
        "retracted": "这条已撤回，不再参与汇总",
    }.get(state)


def _jsonable(value):
    """UUIDs and datetimes as JSON-safe strings; the endpoint is read by scripts."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _entry_link_targets(conn, org_unit_id, company_scope, can_manage):
    """Everything the link form needs: open work items, positions to create one in."""
    items = api.list_open_work_items(conn, org_unit_id=org_unit_id, company_scope=company_scope)
    positions = []
    if can_manage:
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title
               FROM oams.positions p
               WHERE p.status = 'active' AND p.org_unit_id = %s
               ORDER BY p.position_code""", (org_unit_id,)).fetchall()
    return items, positions


def _safe_next(next_url: str | None) -> str:
    # Only same-site paths: an attacker-supplied absolute URL would turn the login
    # redirect into an open redirect.
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/board"
    return next_url


def _login(request: Request, error: str | None = None, next_url: str = "/board"):
    return TEMPLATES.TemplateResponse(request, "login.html",
                                      {"error": error, "next": next_url}, status_code=200)


def _landing_url(request: Request) -> str:
    """The first screen this person can actually read.

    Same order the brand link uses, so a detour never lands on a second 403. The
    navigation dictionary is the server's own answer about what this session may
    open -- asking it here means the detour cannot drift from the menu.
    """
    caps = getattr(request.state, "nav", None) or {}
    for flag, href in (("progress", "/progress"), ("review", "/reviews"),
                       ("admin", "/settings"), ("departments", "/departments")):
        if caps.get(flag):
            return href
    return "/board"


def _denied(request: Request, message: str):
    """Refuse -- by taking the person somewhere they can go, not to a dead end.

    A screen somebody may not open used to answer 403 with a page whose only exit
    was "回看板". That page is a door closing in their face: the menu never shows
    these links, so the only way here is a stale bookmark or a typed URL, and the
    useful answer is the first screen they *can* read.

    The detour says nothing. A line explaining "you are an administrator" or
    "only your departments" is noise on a page whose rows already answer it.

    Form submissions keep the page: a POST that silently redirects would look
    like the click did nothing, and the flash is the only way to say which grant
    is missing.
    """
    if request.method == "GET":
        return RedirectResponse(url=_landing_url(request), status_code=303)
    return TEMPLATES.TemplateResponse(request, "denied.html",
                                      {"message": message, "member": _member(request)}, status_code=403)


_RENDER_LOG = logging.getLogger("oams.render")


def _render_failed(request: Request, *, page: dict[str, Any], exc: RenderError,
                   org_name: str | None, back_href: str, back_label: str):
    """Report a page that cannot be drawn, as a broken page rather than a refusal.

    These used to leave through ``_denied``, which on a GET redirects to the landing
    page and says nothing. So a descriptor the renderer rejects looked exactly like
    a missing permission: the person went asking for a grant that would not have
    helped, and whoever was told "管理员也打不开" had no reason to suspect the
    descriptor. Show the error, the schema version and the owner, and put the same
    facts in the server log, so the failure can be found without reproducing it.

    The status is 500: the page is ours and it is broken -- nothing here was the
    visitor's to fix.
    """
    schema_version = page.get("schema_version") or "0.1"
    _RENDER_LOG.error("render failed: interface=%s slug=%s schema=%s org=%s error=%s",
                      page.get("interface_id"), page.get("slug"), schema_version,
                      org_name, exc)
    return TEMPLATES.TemplateResponse(request, "render_failed.html", {
        "page": page, "org_name": org_name, "schema_version": schema_version,
        "message": str(exc), "back_href": back_href, "back_label": back_label,
        "member": _member(request)}, status_code=500)


def _member(request: Request) -> dict[str, Any] | None:
    return getattr(request.state, "member", None)


def _attach_cookie(response: RedirectResponse, token: str) -> RedirectResponse:
    response.set_cookie(
        key=settings.web_session_cookie,
        value=token,
        httponly=True,      # no script access to the session token
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        # No 'secure': this runs on plain http inside the LAN, where a Secure
        # cookie would never be sent back and login would appear silently broken.
    )
    return response


def _require(request: Request, resource: str, action: str) -> dict[str, Any]:
    """Resolve the browser session and check it against the shared grants."""
    principal = request.state.member
    if principal is None:
        raise _NotSignedIn()
    with connection() as conn:
        if not has_permission(conn, principal["member_id"], resource, action):
            raise _NotPermitted()
    return principal


def _form_token(request: Request, purpose: str = "oams:form:v1") -> str:
    """Bind each POST form to the signed-in session, so a stale or forged token fails."""
    token = request.cookies.get(settings.web_session_cookie, "")
    return hmac.new(token.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _report_csrf(request: Request) -> str:
    return _form_token(request, "oams:report-form:v1")


def _check_form_token(request: Request, supplied: str, purpose: str) -> None:
    # 比字节不比 str：compare_digest 对含非 ASCII 的 str 会抛 TypeError（于是 500 而不是 403，
    # 等于把「令牌错」和「服务端崩」区分开），两边 encode 后走字节分支，恒定时间性质不变。
    submitted = (supplied or "").encode("utf-8")
    expected = _form_token(request, purpose).encode("utf-8")
    if not hmac.compare_digest(submitted, expected):
        raise HTTPException(status_code=403, detail="invalid form token")


class _NotSignedIn(Exception):
    pass


class _NotPermitted(Exception):
    pass




@router.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse(url="/board", status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, token: str | None = None, next: str = "/board"):
    # The link form: /login?token=... signs the browser in and immediately
    # redirects to a clean URL, so the token does not sit in the address bar or
    # in browser history where it would be readable over somebody's shoulder.
    if token:
        return sign_in(request, token, next)
    next_url = _safe_next(next)
    # Design stage: a shortcut sign-in would land back on the page immediately, so
    # skip the detour. An unknown employee_code is reported here instead of
    # looping, because a login screen that silently refuses is the worst outcome.
    if dev_principal(request) is not None:
        return RedirectResponse(url=next_url, status_code=302)
    target = dev_autologin_target(request)
    if target is not None:
        return _login(request, error=f"免登录账号「{target}」不存在或已离职，请改 OAMS_DEV_AUTOLOGIN 或去掉它。", next_url=next_url)
    return _login(request, next_url=next_url)


@router.post("/login", response_class=HTMLResponse)
def sign_in(request: Request, token: str = Form(...), next: str = Form("/board")):
    next_url = _safe_next(next)
    principal = principal_for_token(token)
    if principal is None:
        return _login(request, error="令牌无效、已过期或已被吊销。", next_url=next_url)
    response = RedirectResponse(url=next_url, status_code=302)
    return _attach_cookie(response, token)


@router.post("/logout")
def logout(request: Request):
    principal = request.state.member
    # The shortcut principal has no session row (session_id is NULL), so there is
    # nothing to revoke; revoking blindly would write a NULL subject into the audit
    # trail and make the log unreadable.
    if principal is not None and principal.get("session_id") is not None:
        with connection() as conn:
            revoke_session(conn, principal["session_id"], principal["member_id"])
    if dev_autologin_target(request) is not None:
        # Signing out cannot stick while the shortcut is on, so say so instead of
        # bouncing the browser through /login and back into the same page.
        return _login(request, error="开发阶段免登录已开启（OAMS_DEV_AUTOLOGIN）：退出后会立刻自动登录回来。", next_url="/board")
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(settings.web_session_cookie, path="/")
    return response


@router.post("/dev/identity")
def dev_switch_identity(request: Request, code: str = Form(default=""), next: str = Form(default="/board")):
    """Design-stage identity switch: the badge writes the chosen person to a cookie.

    Only reachable while OAMS_DEV_AUTOLOGIN is on -- otherwise it 404s, so this
    adds nothing to a deployment that forgot to turn the shortcut off. No form
    token on purpose: the shortcut already signs in anybody who can reach the
    port, so a CSRF guard here would only teach people to work around the switch.
    The chosen code is re-checked against the roster, so a hand-edited cookie can
    name nobody but an active member.
    """
    if dev_autologin_target(request) is None:
        raise HTTPException(status_code=404, detail="Not found")
    target = _safe_next(next)
    response = RedirectResponse(url=target, status_code=303)
    wanted = code.strip()
    if not wanted:
        # Clearing the cookie falls back to whatever OAMS_DEV_AUTOLOGIN says.
        response.delete_cookie(DEV_WORKER_COOKIE, path="/")
        return response
    with connection() as conn:
        row = conn.execute(
            "SELECT employee_code FROM oams.workforce_members "
            "WHERE employee_code = %s AND employment_status = 'active'",
            (wanted,)).fetchone()
    if row is None:
        return response
    response.set_cookie(DEV_WORKER_COOKIE, row["employee_code"], max_age=12 * 3600,
                        path="/", httponly=True, samesite="lax")
    return response


def _interface_values(fields, entry):
    """Map a saved entry back onto descriptor field keys for redisplay."""
    values = {}
    if entry is None:
        return values
    for field in fields:
        key = field.get("key")
        if not key:
            continue
        map_to = field.get("map_to")
        if map_to == "display_only":
            values[key] = (entry["extra"] or {}).get(key)
        elif map_to in ("title", "status", "blocked_reason", "progress_value", "note", "evidence"):
            values[key] = entry[map_to]
    return values


def _fill_values_from_form(fields, form) -> dict[str, Any]:
    """What the person just typed, keyed by descriptor field, for redisplay.

    A refused submission has to come back filled in: the server cannot rely on the
    browser's ``required`` (a hand-made POST skips it), and sending the person back
    to an empty form loses the text they may have spent minutes on.
    """
    values: dict[str, Any] = {}
    for field in fields:
        key = field.get("key")
        if not key or key not in form:
            continue
        values[key] = form.get(key)
    return values


def _read_entry_form(fields, status_map, form):
    """Turn posted keys into standard columns plus the department's own extras."""
    standard = {}
    extra = {}
    for field in fields:
        key = field.get("key")
        if not key:
            continue
        posted = [value.strip() for value in form.getlist(key)]
        raw = next((value for value in posted if value), "")
        map_to = field.get("map_to")
        # 一个填报页会给同一个字段渲染好几个控件（每块模块各一行）。空行视为
        # 「没填」：取第一个非空值，而不是第一个控件。必填也只在主记录字段
        # （款号 / 事项）上强制——模块内的必填项若在服务端一并卡住，只填一行
        # 的人永远交不上（端到端试用 R2 / R3）。
        if field.get("required") and not raw and map_to == "title":
            return {}, {}, "「" + str(field.get("label") or key) + "」必填"
        if not raw:
            continue
        if map_to == "display_only":
            extra[key] = raw
            continue
        if map_to == "status":
            mapped = status_map.get(raw)
            if mapped is None:
                return {}, {}, "状态「" + raw + "」未映射，请联系本部门界面维护者补充映射"
            standard["status"] = mapped
            continue
        if map_to == "progress_value":
            try:
                value = int(raw)
            except ValueError:
                return {}, {}, "完成进度必须是数字"
            if not 0 <= value <= 100:
                return {}, {}, "完成进度必须在 0 到 100 之间"
            standard["progress_value"] = value
            continue
        if map_to in ("title", "blocked_reason", "note", "evidence"):
            standard[map_to] = raw
    if standard.get("status") == "blocked" and not (standard.get("blocked_reason") or "").strip():
        return {}, {}, "标记为阻塞时必须写明卡在哪、在等谁"
    return standard, extra, None


def _visible_departments(conn, member_id):
    """Departments this member may work in: their own, or all for company scope."""
    if has_company_scope(conn, member_id):
        return conn.execute(
            """SELECT org_unit_id, org_code, name FROM oams.org_units
               WHERE status = 'active' AND unit_type <> 'company' ORDER BY name""").fetchall()
    scoped = list(active_org_unit_ids(conn, member_id))
    if not scoped:
        return []
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM oams.org_units
           WHERE status = 'active' AND org_unit_id = ANY(%s) ORDER BY name""", (scoped,)).fetchall()


def _department_pages(conn, org_unit_id):
    """Every page of a department, drafts included: a department must see what it is building."""
    return conn.execute(
        """SELECT i.interface_id, i.name, i.status, i.version_no, i.visibility,
                  (SELECT count(*) FROM oams.interface_entries e
                    WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
           FROM oams.interfaces i
           WHERE i.org_unit_id = %s AND i.status <> 'archived'
           ORDER BY (i.status = 'active') DESC, i.name""", (org_unit_id,)).fetchall()


def _resolve_department(conn, principal, org_unit_id):
    """Return the department row, or None when this member has no business there."""
    if not has_company_scope(conn, principal["member_id"]):
        if org_unit_id not in active_org_unit_ids(conn, principal["member_id"]):
            return None
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM oams.org_units
           WHERE org_unit_id = %s AND status = 'active'""", (org_unit_id,)).fetchone()


def _entry_mode_for(status):
    """A page under review collects trial data; only an approved page feeds the company."""
    return "live" if status == "active" else "draft"


@router.get("/departments", response_class=HTMLResponse)
def departments_root(request: Request):
    try:
        principal = _require(request, "workforce.reports", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看部门页面需要 workforce.reports/read 权限。")
    with connection() as conn:
        found = _visible_departments(conn, principal["member_id"])
    if not found:
        return _denied(request, "你目前没有可进入的部门。")
    return RedirectResponse(url="/departments/" + str(found[0]["org_unit_id"]), status_code=302)


@router.get("/departments/{org_unit_id}", response_class=HTMLResponse)
def department_workbench(request: Request, org_unit_id: UUID, saved: str | None = None,
                         error: str | None = None):
    try:
        principal = _require(request, "workforce.reports", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看部门页面需要 workforce.reports/read 权限。")
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        pages = _department_pages(conn, org_unit_id)
        found = _visible_departments(conn, principal["member_id"])
        counts = {row["org_unit_id"]: _department_pages(conn, row["org_unit_id"]) for row in found}
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    live = next((p for p in pages if p["status"] == "active"), pages[0] if pages else None)
    if live is not None:
        return RedirectResponse(url=f"/departments/{org_unit_id}/{live['interface_id']}", status_code=302)
    return TEMPLATES.TemplateResponse(request, "department_workbench.html", {
        "unit": unit, "departments": departments, "pages": pages,
        "current": None, "page": None, "preview": False,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "oams:interface-form:v1")})


@router.get("/departments/{org_unit_id}/new", response_class=HTMLResponse)
def department_new_page(request: Request, org_unit_id: UUID, saved: str | None = None,
                        error: str | None = None):
    """Where a department pastes the page its AI just wrote and sees it immediately."""
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "制作部门页面需要 workforce.reports 读写权限。")
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        positions = conn.execute(
            """SELECT position_id, position_code, title FROM oams.positions
               WHERE org_unit_id = %s AND status = 'active' ORDER BY position_code""",
            (org_unit_id,)).fetchall()
        pages = _department_pages(conn, org_unit_id)
        found = _visible_departments(conn, principal["member_id"])
        counts = {row["org_unit_id"]: _department_pages(conn, row["org_unit_id"]) for row in found}
        bases = _basis_pages(conn, principal["member_id"], org_unit_id)
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    return TEMPLATES.TemplateResponse(request, "department_new_page.html", {
        "unit": unit, "departments": departments, "pages": pages,
        "current": None, "page": None, "positions": positions, "bases": bases,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "oams:page-form:v1")})


@router.post("/departments/{org_unit_id}/new")
def department_create_page(request: Request, org_unit_id: UUID, csrf: str = Form(...),
                           slug: str = Form(...), name: str = Form(...),
                           position_id: str = Form(default=""), descriptor_text: str = Form(...),
                           based_on: str = Form(default="")):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "制作部门页面需要 workforce.reports 读写权限。")
    _check_form_token(request, csrf, "oams:page-form:v1")
    back = f"/departments/{org_unit_id}/new"
    try:
        descriptor = json.loads(descriptor_text)
    except ValueError:
        return RedirectResponse(url=back + "?error=" + urllib.parse.quote("不是合法的 JSON"), status_code=303)
    if not isinstance(descriptor, dict):
        return RedirectResponse(url=back + "?error=" + urllib.parse.quote("描述必须是一个 JSON 对象"), status_code=303)
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        codes = {row["org_code"] for row in conn.execute(
            "SELECT org_code FROM oams.org_units WHERE status = 'active'").fetchall()}
        known_positions = {row["position_code"] for row in conn.execute(
            "SELECT position_code FROM oams.positions WHERE status = 'active'").fetchall()}
        problems = validate_descriptor(descriptor, known_org_unit_codes=codes,
                                       known_position_codes=known_positions)
        if problems:
            return RedirectResponse(url=back + "?error=" + urllib.parse.quote("；".join(problems)[:600]),
                                    status_code=303)
        position = UUID(position_id) if position_id.strip() else None
        # One open proposal at a time per slug. Two drafts of the same page give
        # the reviewer two diffs against one live version and no way to tell which
        # one the department means to ship.
        open_version = conn.execute(
            """SELECT interface_id FROM oams.interfaces
               WHERE org_unit_id = %s AND slug = %s AND status IN ('draft', 'pending')
               ORDER BY version_no DESC LIMIT 1""", (org_unit_id, slug.strip())).fetchone()
        if open_version is not None:
            return RedirectResponse(
                url=f"/departments/{org_unit_id}/{open_version['interface_id']}?error="
                    + urllib.parse.quote("这个页面已经有一版在改或待审核，先把那一版处理掉"),
                status_code=303)
        # The live version keeps serving while the next one is written and
        # reviewed. Archiving it here would blank the department's page for as
        # long as review takes -- and if the draft were then abandoned, forever.
        live = conn.execute(
            """SELECT interface_id, version_no FROM oams.interfaces
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (org_unit_id, slug.strip())).fetchone()
        # A page may also be built on another department's live screen ("roll out
        # what sales already runs"). That is a heavier claim than copying your own
        # last version -- it speaks for somebody else's page -- so the reference is
        # recorded and the review goes up one level (review.needs_company_review).
        basis = live["interface_id"] if live is not None else None
        wanted = based_on.strip()
        if wanted:
            try:
                source_id = UUID(wanted)
            except ValueError:
                return RedirectResponse(url=back + "?error=" + urllib.parse.quote("来源页面编号不对"),
                                        status_code=303)
            source = conn.execute(
                """SELECT interface_id, org_unit_id, status, visibility FROM oams.interfaces
                    WHERE interface_id = %s""", (source_id,)).fetchone()
            openable = source is not None and source["status"] == "active" and (
                source["visibility"] == "public"
                or source["org_unit_id"] in active_org_unit_ids(conn, principal["member_id"]))
            if not openable:
                # "不存在" and "不开放给你" are one sentence on purpose: telling them
                # apart would confirm that a page you may not see exists.
                return RedirectResponse(
                    url=back + "?error=" + urllib.parse.quote("这个来源页面不存在，或者不开放给你"),
                    status_code=303)
            if source["org_unit_id"] == org_unit_id:
                return RedirectResponse(
                    url=back + "?error=" + urllib.parse.quote(
                        "本部门上一版会自动作为基础，不用指定来源"),
                    status_code=303)
            basis = source["interface_id"]
        version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 FROM oams.interfaces
               WHERE org_unit_id = %s AND slug = %s""", (org_unit_id, slug.strip())).fetchone()["?column?"]
        # A page a department just wrote starts unapproved: it must be seen and
        # filled inside the department before it can claim to speak for the company.
        row = conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    position_id, version_no, based_on, created_by, updated_by)
               VALUES (%s, %s, %s, 'draft', 'department', %s, %s, %s, %s, %s, %s, %s)
               RETURNING interface_id""",
            (org_unit_id, slug.strip(), name.strip(), descriptor.get("schema_version", "0.1"),
             Jsonb(descriptor), position, version, basis,
             principal["member_id"], principal["member_id"])).fetchone()
        api.audit(conn, principal["member_id"], "interface.created", "interface", row["interface_id"],
                  "created inside department workbench",
                  {"slug": slug.strip(), "version_no": version,
                   "based_on": str(basis) if basis else None})
    return RedirectResponse(url=f"/departments/{org_unit_id}/{row['interface_id']}?saved=1", status_code=303)


@router.get("/departments/{org_unit_id}/{interface_id}", response_class=HTMLResponse)
def department_page(request: Request, org_unit_id: UUID, interface_id: UUID,
                    saved: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门页面需要 workforce.reports/read 与 submit 权限。")
    return _workbench_page(request, principal, org_unit_id, interface_id,
                           saved=saved, error=error)


def _workbench_page(request: Request, principal: dict[str, Any], org_unit_id: UUID,
                    interface_id: UUID, *, saved: str | None = None, error: str | None = None,
                    values: dict[str, Any] | None = None, status_code: int = 200):
    """Draw the department workbench for one page.

    ``values`` overrides what the fields show. A submission the server refused comes
    back through here with what the person typed -- re-rendering the last *saved*
    entry instead would quietly throw their words away.
    """
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        page = conn.execute(
            """SELECT * FROM oams.interfaces
               WHERE interface_id = %s AND org_unit_id = %s AND status <> 'archived'""",
            (interface_id, org_unit_id)).fetchone()
        if page is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        mode = _entry_mode_for(page["status"])
        descriptor = page["descriptor"]
        # The rollup view of the same page: every entry with the state that says
        # whether it feeds the company, from api.ROLLUP_STATE_SQL -- the one place
        # the three conditions are written, so the badge cannot disagree with the
        # number the board shows.
        recent = _with_rollup_labels(api.page_entries(conn, interface_id))[:12]
        rollup = api.build_department_aggregates(conn, org_unit_id, descriptor=descriptor,
                                                interface_id=interface_id)
        # The link form: the work items an entry may be attached to, and the
        # positions a manager may open one in. Without this a department can never
        # set work_item_id, and its honest work stays out of the company number.
        company_scope = has_company_scope(conn, principal["member_id"])
        can_manage_work_items = has_permission(
            conn, principal["member_id"], "workforce.work_items", "manage")
        link_items, link_positions = _entry_link_targets(
            conn, org_unit_id, company_scope, can_manage_work_items)
        pages = _department_pages(conn, org_unit_id)
        found = _visible_departments(conn, principal["member_id"])
        counts = {row["org_unit_id"]: _department_pages(conn, row["org_unit_id"]) for row in found}
        latest = conn.execute(
            """SELECT * FROM oams.interface_entries
               WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
               ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
        # A draft, or a page under review, does not replace the live one: name the
        # version still serving the company, or the department will believe the
        # screen it is editing is the screen the company sees.
        live_version = None
        if page["status"] != "active":
            live_version = conn.execute(
                """SELECT interface_id, version_no, updated_at FROM oams.interfaces
                   WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
                (org_unit_id, page["slug"])).fetchone()
    descriptor = page["descriptor"]
    fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
    if values is None:
        values = _interface_values(fields, latest)
    try:
        rendered = render_interface(descriptor, values=values)
    except RenderError as exc:
        return _render_failed(request, page=page, exc=exc, org_name=unit["name"],
                              back_href=f"/departments/{org_unit_id}", back_label="返回部门")
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    return TEMPLATES.TemplateResponse(request, "department_workbench.html", {
        "unit": unit, "departments": departments, "pages": pages,
        "current": page, "page": page, "rendered": rendered, "recent": recent,
        "preview": page["status"] != "active", "draft_mode": page["status"] != "active",
        "page_status_label": {"draft": "待提交审核", "pending": "审核中"}.get(page["status"], page["status"]),
        "live_version": live_version,
        "review_code_label": (review_rules.rejection_label(page.get("review_code"))
                              if page.get("review_code") else None),
        "member": _member(request), "saved": saved, "error": error,
        "status_labels": STATUS_LABELS,
        # Rollup: the department's own numbers, why they are what they are, and the
        # link form that moves an entry from "unlinked" into "rolled".
        "rollup": rollup,
        "rollup_counts": rollup.get("counts") or {},
        "link_items": link_items,
        "link_positions": link_positions,
        "link_purpose": ENTRY_LINK_PURPOSE,
        "link_csrf": _form_token(request, ENTRY_LINK_PURPOSE),
        "work_item_csrf": _form_token(request, "oams:work-item-form:v1"),
        "can_manage_work_items": can_manage_work_items,
        # The page holds two different forms (fill it in, submit it for review),
        # so it carries a token for each; a token minted for one must not pass the other.
        "csrf": _form_token(request, "oams:interface-form:v1"),
        "review_csrf": _form_token(request, "oams:page-form:v1")}, status_code=status_code)


@router.post("/departments/{org_unit_id}/{interface_id}")
async def department_page_submit(request: Request, org_unit_id: UUID, interface_id: UUID,
                                 csrf: str = Form(...)):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门页面需要 workforce.reports/read 与 submit 权限。")
    _check_form_token(request, csrf, "oams:interface-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        page = conn.execute(
            """SELECT * FROM oams.interfaces
               WHERE interface_id = %s AND org_unit_id = %s AND status <> 'archived'""",
            (interface_id, org_unit_id)).fetchone()
        if page is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        descriptor = page["descriptor"]
        fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
        form = await request.form()
        standard, extra, problem = _read_entry_form(fields, descriptor.get("status_map", {}), form)
        if problem is not None:
            return _workbench_page(request, principal, org_unit_id, interface_id, error=problem,
                                   values=_fill_values_from_form(fields, form), status_code=400)
        mode = _entry_mode_for(page["status"])
        columns = dict(standard)
        columns["extra"] = Jsonb(extra)
        columns["reported_by"] = principal["member_id"]
        columns["entry_mode"] = mode
        keys = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        entry = conn.execute(
            "INSERT INTO oams.interface_entries (interface_id, " + keys + ") VALUES (%s, " + placeholders + ")"
            + " RETURNING entry_id, title, status, note, evidence, reported_at",
            (interface_id, *columns.values())).fetchone()
        api.audit(conn, principal["member_id"], "interface_entry.created", "interface_entry",
                  entry["entry_id"], "submitted through department page (" + mode + ")", entry)
    return RedirectResponse(url=base + "?saved=1", status_code=303)


@router.post("/departments/{org_unit_id}/{interface_id}/entries/{entry_id}/link")
def entry_link(request: Request, org_unit_id: UUID, interface_id: UUID, entry_id: UUID,
               csrf: str = Form(...), work_item_id: str = Form("")):
    """Attach one entry to a work item -- the only way an entry enters a company number.

    The three rollup conditions are enforced in SQL (api.ROLLUP_PREDICATE), so
    nothing here re-decides them: this writes the work_item_id that the third
    condition reads, and nothing else. Empty work_item_id unlinks again, which is
    a legitimate correction, not a way to hide a number.
    """
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "关联工作项需要 workforce.reports/read 与 submit 权限。")
    _check_form_token(request, csrf, ENTRY_LINK_PURPOSE)
    base = f"/departments/{org_unit_id}/{interface_id}"
    try:
        target = UUID(work_item_id) if work_item_id.strip() else None
    except ValueError:
        return RedirectResponse(url=base + "?error=work-item-invalid", status_code=303)
    try:
        with connection() as conn:
            unit = _resolve_department(conn, principal, org_unit_id)
            if unit is None:
                return _denied(request, "你不在这个部门的范围内。")
            entry = conn.execute(
                """SELECT entry_id, interface_id, title FROM oams.interface_entries
                   WHERE entry_id = %s AND interface_id = %s""", (entry_id, interface_id)).fetchone()
            if entry is None:
                return _denied(request, "找不到这条汇报记录。")
            if target is not None:
                # The work item must exist and sit inside a department this member
                # may report on; otherwise the form would link across departments.
                item = conn.execute(
                    """SELECT w.work_item_id, w.org_unit_id FROM oams.work_items w
                       JOIN oams.positions p USING (position_id)
                       WHERE w.work_item_id = %s AND p.org_unit_id = %s""",
                    (target, org_unit_id)).fetchone()
                if item is None:
                    return RedirectResponse(url=base + "?error=work-item-invalid", status_code=303)
            conn.execute(
                "UPDATE oams.interface_entries SET work_item_id = %s WHERE entry_id = %s",
                (target, entry_id))
            api.audit(conn, principal["member_id"],
                      "interface_entry.unlinked" if target is None else "interface_entry.linked",
                      "interface_entry", entry_id,
                      "attached to work item " + (str(target) or "(none)"), entry)
    except ForeignKeyViolation:
        return RedirectResponse(url=base + "?error=work-item-invalid", status_code=303)
    return RedirectResponse(url=base + "?saved=linked", status_code=303)


@router.get("/departments/{org_unit_id}/{interface_id}/entries.json")
def entry_trace(request: Request, org_unit_id: UUID, interface_id: UUID,
               metric_key: str | None = None):
    """Which entries produced a number: the traceability the concept doc asks for.

    A company figure that cannot be opened is a figure nobody trusts. This returns
    the same rows api used to aggregate, so a manager can click a number and read
    the entries behind it -- including the ones that are NOT in it, with the
    reason, because "why is this smaller than what we did" is the real question.
    """
    try:
        principal = _require(request, "workforce.reports", "read")
    except _NotSignedIn:
        return JSONResponse(status_code=401, content={"available": False, "reason": "not signed in"})
    except _NotPermitted:
        return JSONResponse(status_code=403, content={"available": False, "reason": "no permission"})
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return JSONResponse(status_code=403, content={"available": False, "reason": "not in this department"})
        data = api.rollup_entries(conn, interface_id, metric_key)
    return JSONResponse(content=_jsonable(data))


@router.post("/departments/{org_unit_id}/{interface_id}/submit-review")
def department_submit_review(request: Request, org_unit_id: UUID, interface_id: UUID,
                             review_csrf: str = Form(...)):
    """A department declares a page finished and asks the company to look at it."""
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "提交审核需要 workforce.reports 读写权限。")
    _check_form_token(request, review_csrf, "oams:page-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        prior = conn.execute(
            """SELECT interface_id, status FROM oams.interfaces
               WHERE interface_id = %s AND org_unit_id = %s AND status <> 'archived'""",
            (interface_id, org_unit_id)).fetchone()
        if prior is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        if prior["status"] == "pending":
            return RedirectResponse(url=base + "?error=" + urllib.parse.quote("这个页面已经提交，正在等审核"),
                                    status_code=303)
        if prior["status"] == "active":
            return RedirectResponse(url=base + "?error=" + urllib.parse.quote("这个页面已经启用，无需再次提交"),
                                    status_code=303)
        conn.execute(
            """UPDATE oams.interfaces
                  SET status = 'pending', review_stage = 'department', review_note = NULL, updated_at = now()
               WHERE interface_id = %s""", (interface_id,))
        api.audit(conn, principal["member_id"], "interface.review_requested", "interface", interface_id,
                  "department submitted the page for review", {"from": prior["status"], "to": "pending"})
    return RedirectResponse(url=base + "?saved=review", status_code=303)


@router.post("/departments/{org_unit_id}/{interface_id}/withdraw-review")
def department_withdraw_review(request: Request, org_unit_id: UUID, interface_id: UUID,
                               review_csrf: str = Form(...)):
    """Take back a review request that has not been decided yet.

    Only a page still waiting may be withdrawn: once a reviewer has approved or
    returned it, the decision is on record and reversing it would rewrite history
    instead of correcting a mistake.
    """
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "撤回审核需要 workforce.reports 读写权限。")
    _check_form_token(request, review_csrf, "oams:page-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        prior = conn.execute(
            """SELECT interface_id, status, reviewed_at FROM oams.interfaces
               WHERE interface_id = %s AND org_unit_id = %s AND status <> 'archived' FOR UPDATE""",
            (interface_id, org_unit_id)).fetchone()
        if prior is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        if prior["status"] != "pending":
            return RedirectResponse(
                url=base + "?error=" + urllib.parse.quote("只有正在等审核的页面可以撤回"), status_code=303)
        if prior["reviewed_at"] is not None:
            return RedirectResponse(
                url=base + "?error=" + urllib.parse.quote("审核已给出意见，不能撤回；请按意见修改后再提交"),
                status_code=303)
        conn.execute(
            """UPDATE oams.interfaces SET status = 'draft', review_stage = 'department', updated_at = now()
               WHERE interface_id = %s""", (interface_id,))
        api.audit(conn, principal["member_id"], "interface.review_withdrawn", "interface", interface_id,
                  "department withdrew the review request", {"from": "pending", "to": "draft"})
    return RedirectResponse(url=base + "?saved=withdrawn", status_code=303)

def _review_bundle(conn, row):
    """The diff a reviewer reads: this version against the one it was written on.

    based_on is what the department started from, so it is the honest reference
    even if someone else's version went live in the meantime. Falling back to
    "whatever is live now" keeps old rows readable; a page with neither is a
    first version, and review_rules says so instead of pretending nothing changed.
    """
    reference = None
    if row.get("based_on"):
        reference = conn.execute(
            "SELECT descriptor, version_no FROM oams.interfaces WHERE interface_id = %s",
            (row["based_on"],)).fetchone()
    if reference is None:
        reference = conn.execute(
            """SELECT descriptor, version_no FROM oams.interfaces
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (row["org_unit_id"], row["slug"])).fetchone()
    old_descriptor = reference["descriptor"] if reference is not None else None
    bundle = review_rules.review_bundle(old_descriptor, row.get("descriptor"))
    bundle["reference_version"] = reference["version_no"] if reference is not None else None
    return bundle


def _department_reviewers(conn) -> dict[Any, set[Any]]:
    """Who can sign the first level, per department -- one query, not one per page.

    The queue needs this for a single question: for this page, is there anybody
    else who could do the department's half?  When the answer is nobody the
    platform has to be able to stand in (see `_review_refusal`); otherwise a page
    submitted by the only reviewer of its department waits forever.

    Grants carrying `effect='deny'` still count as reviewers here, although
    `has_permission` would refuse them. A wrong "somebody else can do it" is
    recoverable (ask a colleague); a wrong "nobody can do it" locks the page.
    """
    rows = conn.execute(
        """WITH reviewer_members AS (
               SELECT DISTINCT a.member_id
                 FROM oams.permission_grants g
                 JOIN oams.position_assignments a
                   ON (g.subject_type = 'position' AND a.position_id = g.position_id)
                   OR (g.subject_type = 'member' AND a.member_id = g.member_id)
                WHERE g.resource = 'workforce.interfaces' AND g.action = 'review'
                  AND g.revoked_at IS NULL AND g.scope_type = 'all'
                  AND g.valid_from <= now()
                  AND (g.valid_until IS NULL OR now() < g.valid_until)
                  AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
                  AND (a.ends_at IS NULL OR now() < a.ends_at))
           SELECT DISTINCT r.member_id, p.org_unit_id
             FROM reviewer_members r
             JOIN oams.position_assignments a
               ON a.member_id = r.member_id
              AND a.status IN ('active', 'scheduled') AND a.starts_at <= now()
              AND (a.ends_at IS NULL OR now() < a.ends_at)
             JOIN oams.positions p
               ON p.position_id = a.position_id AND p.status = 'active'""").fetchall()
    by_unit: dict[Any, set[Any]] = {}
    for row in rows:
        by_unit.setdefault(row["org_unit_id"], set()).add(row["member_id"])
    return by_unit


#: What a member may sign off, and where -- resolved once, read everywhere.
def _review_powers(conn, principal: dict[str, Any]) -> dict[str, Any]:
    """Two rights, two jobs, and neither implies the other.

    `review` lets someone sign off the pages of the departments their own
    positions sit in; `review_company` lets the platform sign off anything that
    reached the final stage. A department reviewer's range is deliberately
    `active_org_unit_ids` and not `administered_org_unit_ids`: administering a
    department is a different job from reviewing its pages, and most reviewers
    administer nothing at all.
    """
    member_id = principal["member_id"]
    department = has_permission(conn, member_id, "workforce.interfaces", "review")
    company = has_permission(conn, member_id, "workforce.interfaces", "review_company")
    units = active_org_unit_ids(conn, member_id) if department else set()
    # Only the platform needs the roster, and only to find out that a department
    # has nobody left to ask; a department reviewer never acts outside their own.
    reviewers = _department_reviewers(conn) if company else {}
    return {"member_id": member_id, "department": department, "company": company,
            "units": units, "reviewers": reviewers}


def _stands_in(powers: dict[str, Any], page: Any) -> bool:
    """Whether the platform has to take the first level, nobody else being left.

    The page's own author counts out on purpose: a reviewer who wrote the version
    cannot also sign it, so they do not save the department from needing somebody
    else -- that is the whole point of keeping authors away from their own page.
    """
    if not powers["company"]:
        return False
    others = powers["reviewers"].get(page["org_unit_id"], set())
    return not (others - {powers["member_id"], page["updated_by"]})


def _review_refusal(powers: dict[str, Any], page: Any) -> str | None:
    """Why this member may not decide this page, or None when they may.

    The queue asks it to decide whether to draw buttons and the two POST routes
    ask it before writing: one rule, so a page can never show "approve" and then
    refuse the click. Authors are refused on purpose -- a page nobody else looked
    at is not a reviewed page.

    The last branch is the platform standing in for a department that has nobody
    left to ask. It is not a shortcut: it only opens when every other reviewer of
    that department is the author, and the audit records that no department
    signature happened.
    """
    if page["status"] != "pending":
        return "这个页面不在待审核状态"
    if page["updated_by"] is not None and page["updated_by"] == powers["member_id"]:
        return "自己提交的页面要由别人审"
    if page["review_stage"] == "company":
        return None if powers["company"] else "公司终审需要 workforce.interfaces/review_company 权限"
    if powers["department"] and page["org_unit_id"] in powers["units"]:
        return None
    if _stands_in(powers, page):
        return None
    if powers["department"]:
        return "这个页面不在你所在部门的范围内"
    return "初审需要 workforce.interfaces/review 权限"


def _cross_department(conn, page: Any) -> bool:
    """Whether this version was written on top of another department's page.

    The descriptor does not carry the owner of the version it started from, so the
    honest answer comes from the row `based_on` points at; a page with no
    reference is its own department's work and no escalation on that account.
    """
    if not page["based_on"]:
        return False
    reference = conn.execute(
        "SELECT org_unit_id FROM oams.interfaces WHERE interface_id = %s",
        (page["based_on"],)).fetchone()
    return reference is not None and reference["org_unit_id"] != page["org_unit_id"]


def _acting_stage(powers: dict[str, Any], page: Any) -> str:
    """Which signature this click actually is -- written into the audit record.

    `department` and `company` are the two normal ones; `company_proxy` is the
    platform taking a first level because the department had nobody else. The
    record has to say so: the department never signed that version, and a later
    reader must not be told otherwise.
    """
    if page["review_stage"] == "company":
        return "company"
    if powers["department"] and page["org_unit_id"] in powers["units"]:
        return "department"
    return "company_proxy"


def _basis_pages(conn, member_id, org_unit_id: UUID) -> list[Any]:
    """Other departments' live pages this person may start a new page from.

    Only pages the caller can already open: a picker that lists pages it would
    then refuse invites people to click a dead option, and a list that names
    hidden pages leaks exactly the fact a 404 is there to withhold.
    """
    mine = list(active_org_unit_ids(conn, member_id))
    return conn.execute(
        """SELECT i.interface_id, i.name, i.visibility, o.name AS org_name
             FROM oams.interfaces i JOIN oams.org_units o USING (org_unit_id)
            WHERE i.status = 'active' AND i.org_unit_id <> %s
              AND (i.visibility = 'public' OR i.org_unit_id = ANY(%s))
            ORDER BY o.name, i.name""", (org_unit_id, mine)).fetchall()


def _employee_code_of(conn, principal: dict[str, Any]) -> str | None:
    """The employee code behind whoever is clicking.

    A browser or bearer session carries the member id but not the code, and the
    signature has to name somebody the record can be read against later -- so the
    code is looked up in the roster instead of being guessed from the id.
    """
    code = principal.get("employee_code")
    if isinstance(code, str) and code:
        return code
    row = conn.execute(
        "SELECT employee_code FROM oams.workforce_members WHERE member_id = %s",
        (principal["member_id"],)).fetchone()
    return row["employee_code"] if row else None


def _member_id_for_code(conn, employee_code: str) -> UUID | None:
    """The member behind an employee code, or None when nobody answers to it."""
    row = conn.execute(
        """SELECT member_id FROM oams.workforce_members
            WHERE employee_code = %s AND employment_status = 'active'""",
        (employee_code,)).fetchone()
    return row["member_id"] if row else None


def _active_employee_codes(conn) -> set[str]:
    """Every employee code that currently works here.

    The approval gate needs this to tell a real signature from a name the author
    typed: `recorded_approval_gaps` compares `reviewed_by` against this list, so
    a hand-written stamp naming nobody (or naming somebody who left) is refused
    at the same door that refuses a missing stamp.
    """
    rows = conn.execute(
        """SELECT employee_code FROM oams.workforce_members
            WHERE employment_status = 'active' AND employee_code IS NOT NULL""").fetchall()
    return {row["employee_code"] for row in rows if row["employee_code"]}


def _activate_page(conn, principal: dict[str, Any], page: Any, note: str,
                   acting: str = "department") -> str | None:
    """Make an approved page the live screen for its department slug.

    Returns None when the page went live, or a sentence explaining why the
    approval could not be written into it. That second case is not expected to
    happen; it is here because publishing a page whose own descriptor denies it
    was approved is precisely the lie this round exists to remove, and refusing
    is cheaper than explaining later.
    """
    # Which level this signature belongs to, spelled out instead of inferred from
    # `acting` later: a stand-in still signs the *department's* half, and a reader
    # of the log must not have to know the vocabulary to see that.
    signed_level = "company" if acting == "company" else "department"
    # The signature is written here, not read from the document. An author can
    # type "approved" into their own JSON; until this line existed nothing in
    # the system could tell that apart from a real sign-off, which is what made
    # "declare a public page" impossible to do honestly.
    signer = _employee_code_of(conn, principal)
    if not signer:
        return "这个账号没有工号，签不了批：批准要写进页面描述，而那上面只认在职成员工号"
    stamped = review_rules.stamp_approval(
        page["descriptor"] or {},
        reviewed_by=signer,
        stage=signed_level,
        version_no=page["version_no"],
    )
    gaps = recorded_approval_gaps(stamped, known_employee_codes=_active_employee_codes(conn))
    if gaps:
        return "批准没能写进页面描述，页面没有上线：" + "；".join(gaps[:3])
    # Approving replaces whatever was live for this slug: a department must never
    # end up with two pages claiming to be the current one.
    conn.execute(
        """UPDATE oams.interfaces SET status = 'archived', updated_at = now()
           WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
        (page["org_unit_id"], page["slug"]))
    # The audience the department asked for only takes effect now: a public
    # template has to be signed off, so the row keeps 'department' while the page
    # waits and the approval is what promotes it.
    requested_visibility = review_rules.visibility_of(page["descriptor"] or {})
    # The row records the level that actually signed, not the level a reviewer
    # happens to be sitting at: a department approval that goes straight live must
    # not read as if the company had signed it, and the row, the stamp inside the
    # descriptor and the audit entry have to tell the same story.
    conn.execute(
        """UPDATE oams.interfaces
              SET status = 'active', review_stage = %s,
                  reviewed_by = %s, reviewed_at = now(),
                  review_note = %s, visibility = COALESCE(%s, visibility),
                  descriptor = %s, updated_at = now()
            WHERE interface_id = %s""",
        (signed_level, principal["member_id"], note.strip() or None,
         requested_visibility if requested_visibility in ("department", "public") else None,
         Jsonb(stamped), page["interface_id"]))
    # The approval records what was actually reviewed, so "why did the number move
    # on the 12th" can be answered from the audit log alone.
    bundle = _review_bundle(conn, page)
    summary = bundle["summary"]["headline"] if bundle else ""
    api.audit(conn, principal["member_id"], "interface.approved", "interface", page["interface_id"],
              (note.strip() or "review approved") + (f"｜{summary}" if summary else ""),
              {"to": "active", "version_no": page["version_no"], "stage": signed_level,
               "acting": acting, "changes": bundle["summary"]["total"] if bundle else 0})
    return None


@router.get("/reviews", response_class=HTMLResponse)
def review_queue(request: Request, saved: str | None = None, error: str | None = None):
    """Two queues, because two people sign: the department first, the platform last.

    A reviewer sees what they can decide -- a department lead the first-level
    pages of their own departments, the platform what the departments passed up --
    plus, for the platform, a read-only list of everything still pending, so
    "why is this page not live yet" has an answer without anyone guessing.
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    with connection() as conn:
        powers = _review_powers(conn, principal)
        if not (powers["department"] or powers["company"]):
            return _denied(request, "审核部门页面需要 workforce.interfaces/review 或 review_company 权限。")
        awaiting = conn.execute(
            """SELECT i.interface_id, i.name, i.slug, i.version_no, i.visibility, i.schema_version,
                      i.descriptor, i.based_on, i.status, i.review_stage, i.updated_by,
                      o.name AS org_name, o.org_unit_id, p.title AS position_title,
                      m.display_name AS author, i.updated_at
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.positions p ON p.position_id = i.position_id
               LEFT JOIN oams.workforce_members m ON m.member_id = i.updated_by
               WHERE i.status = 'pending'
               ORDER BY (i.review_stage = 'company') DESC, i.updated_at""").fetchall()
        decided = conn.execute(
            """SELECT i.interface_id, i.name, i.status, i.review_note, i.review_code, o.name AS org_name,
                      m.display_name AS reviewer, i.reviewed_at
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.workforce_members m ON m.member_id = i.reviewed_by
               WHERE i.reviewed_at IS NOT NULL
               ORDER BY i.reviewed_at DESC LIMIT 10""").fetchall()
    mine = [row for row in awaiting if _review_refusal(powers, row) is None]
    others = [row for row in awaiting if powers["company"] and _review_refusal(powers, row) is not None]
    # Every pending page is read as a diff against the version it was written
    # against, not as a whole page: that is the only part a human still has to
    # think about, and the part where a quiet re-mapping would otherwise slip by.
    with connection() as conn:
        review_cards = [dict(row, bundle=_review_bundle(conn, row), acting=_acting_stage(powers, row))
                        for row in mine]
    # The decision list reads a code, not prose: "what do departments keep getting
    # wrong" has to be answerable by counting, not by re-reading old notes.
    decided_cards = [dict(row, code_label=review_rules.rejection_label(row["review_code"]))
                     for row in decided]
    return TEMPLATES.TemplateResponse(request, "reviews.html", {
        "pending": review_cards, "others": others, "decided": decided_cards, "member": principal,
        "powers": powers, "saved": saved, "error": error, "reasons": review_rules.REJECTION_CHOICES,
        "csrf": _form_token(request, "oams:review-form:v1")})


@router.post("/reviews/{interface_id}/approve")
def review_approve(request: Request, interface_id: UUID, csrf: str = Form(...),
                   note: str = Form(default="")):
    """Sign a page off -- at the department, or at the company, as the page asks.

    A department's yes on a page that only it uses is the whole decision and the
    page goes live. A page that leaves the department -- public, claiming a
    company figure, or written on another department's page -- keeps waiting
    after that yes, now at the company stage: the department passed it, the
    platform still has to agree.

    The first-level pass records no `reviewed_by`/`reviewed_at`: those two mean "a
    decision was given and the department has to act on it", and nothing was
    refused, so the page stays withdrawable and its note stays empty.
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    _check_form_token(request, csrf, "oams:review-form:v1")
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = conn.execute(
            """SELECT i.interface_id, i.org_unit_id, i.slug, i.status, i.version_no, i.descriptor,
                      i.based_on, i.review_stage, i.updated_by
               FROM oams.interfaces i WHERE i.interface_id = %s FOR UPDATE""",
            (interface_id,)).fetchone()
        if page is None:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("找不到这个页面"), status_code=303)
        refusal = _review_refusal(powers, page)
        if refusal is not None:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote(refusal), status_code=303)
        acting = _acting_stage(powers, page)
        if page["review_stage"] == "company":
            failed = _activate_page(conn, principal, page, note, acting=acting)
            if failed:
                return RedirectResponse(url="/reviews?error=" + urllib.parse.quote(failed), status_code=303)
            return RedirectResponse(url="/reviews?saved=approved", status_code=303)
        escalate = review_rules.needs_company_review(
            page["descriptor"] or {}, cross_department=_cross_department(conn, page))
        if not escalate:
            failed = _activate_page(conn, principal, page, note, acting=acting)
            if failed:
                return RedirectResponse(url="/reviews?error=" + urllib.parse.quote(failed), status_code=303)
            return RedirectResponse(url="/reviews?saved=approved", status_code=303)
        conn.execute(
            """UPDATE oams.interfaces SET review_stage = 'company', updated_at = now()
               WHERE interface_id = %s""", (interface_id,))
        api.audit(conn, principal["member_id"], "interface.review_stage_passed", "interface", interface_id,
                  note.strip() or "department passed the page to the company stage",
                  {"from": "department", "to": "company", "version_no": page["version_no"],
                   "stage": "department", "acting": acting})
    return RedirectResponse(url="/reviews?saved=passed", status_code=303)


@router.post("/reviews/{interface_id}/reject")
def review_reject(request: Request, interface_id: UUID, csrf: str = Form(...),
                  note: str = Form(...), reason_code: str = Form(default="")):
    """Send a page back with a reason; the department keeps its trial entries."""
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    _check_form_token(request, csrf, "oams:review-form:v1")
    if not note.strip():
        return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("退回必须写明原因"), status_code=303)
    # A code is optional for scripts and older forms but never guessed: an empty
    # code stays NULL and shows as 未分类, because inventing a class from prose
    # would put a fact in the record that the reviewer never stated.
    code = (reason_code or "").strip() or None
    if code is not None and not review_rules.is_rejection_code(code):
        return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("未知的退回原因类型"),
                                status_code=303)
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = conn.execute(
            """SELECT interface_id, org_unit_id, status, review_stage, updated_by
               FROM oams.interfaces WHERE interface_id = %s FOR UPDATE""",
            (interface_id,)).fetchone()
        if page is None:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("找不到这个页面"), status_code=303)
        refusal = _review_refusal(powers, page)
        if refusal is not None:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote(refusal), status_code=303)
        # A returned page starts over at the first level: whatever the company said
        # about it, the department's own lead reviews the fix first.
        conn.execute(
            """UPDATE oams.interfaces
                  SET status = 'draft', review_stage = 'department',
                      reviewed_by = %s, reviewed_at = now(),
                      review_note = %s, review_code = %s, updated_at = now()
                WHERE interface_id = %s""",
            (principal["member_id"], note.strip(), code, interface_id))
        api.audit(conn, principal["member_id"], "interface.rejected", "interface", interface_id,
                  note.strip(), {"to": "draft", "review_code": code,
                                 "from_stage": page["review_stage"],
                                 "acting": _acting_stage(powers, page)})
    return RedirectResponse(url="/reviews?saved=rejected", status_code=303)


@router.get("/interfaces", response_class=HTMLResponse)
def interfaces(request: Request, saved: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.reports", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看部门界面需要 workforce.reports/read 权限。")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        rows = conn.execute(
            """SELECT i.interface_id, i.slug, i.name, i.visibility, i.version_no,
                      o.name AS org_name, p.title AS position_title,
                      (SELECT count(*) FROM oams.interface_entries e
                        WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.positions p ON p.position_id = i.position_id
               WHERE i.status = 'active' AND (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""", (company_scope, scoped)).fetchall()
    return TEMPLATES.TemplateResponse(request, "interfaces.html", {
        "interfaces": rows, "member": _member(request), "saved": saved, "error": error})



@router.get("/interfaces/board", response_class=HTMLResponse)
def interface_board(request: Request, stale_days: int = Query(default=7, ge=1, le=365)):
    """Manager view: what each department last reported through its own screen."""
    try:
        principal = _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看汇报中心需要工作项查看权限。")
    member_id = principal["member_id"]
    with connection() as conn:
        # What you can see here follows your level: everything, your own
        # departments, or only the entries you reported yourself.
        level = admin_level(conn, member_id)
        where = "i.status = 'active'"
        # Staff look at their own submissions, so the "latest entry" they read has
        # to be the latest *theirs* -- otherwise a colleague's newer entry would
        # hide their own from them.
        entry_filter = ""
        params: list[Any] = []
        if level == ROLE_DEPARTMENT:
            where = where + " AND i.org_unit_id = ANY(%s::uuid[])"
            params.append(list(administered_org_unit_ids(conn, member_id)))
        elif level == ROLE_STAFF:
            entry_filter = " AND reported_by = %s"
            where = where + " AND e.entry_id IS NOT NULL"
            params.append(member_id)
        rows = conn.execute(
            f"""SELECT i.interface_id, i.name, i.slug, o.name AS org_name, p.title AS position_title,
                      e.title, e.status, e.blocked_reason, e.needs_decision, e.progress_value,
                      e.note, e.evidence, e.reported_at, m.display_name AS reporter,
                      e.extra
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.positions p ON p.position_id = i.position_id
               LEFT JOIN LATERAL (
                   SELECT * FROM oams.interface_entries
                    WHERE interface_id = i.interface_id AND status_entry = 'active'
                      AND entry_mode = 'live'{entry_filter}
                    ORDER BY reported_at DESC, entry_id DESC LIMIT 1
               ) e ON true
               LEFT JOIN oams.workforce_members m ON m.member_id = e.reported_by
               WHERE {where}
               ORDER BY o.name, i.name""", params).fetchall()
    return TEMPLATES.TemplateResponse(request, "interface_board.html", {
        "rows": rows, "member": _member(request), "stale_days": stale_days,
        "status_labels": STATUS_LABELS})

@router.get("/interfaces/{interface_id}", response_class=HTMLResponse)
def interface_fill(request: Request, interface_id: UUID, saved: str | None = None,
                   error: str | None = None):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门界面需要 workforce.reports/read 与 submit 权限。")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        row = conn.execute(
            """SELECT i.*, o.name AS org_name FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               WHERE i.interface_id = %s AND i.status = 'active'
                 AND (%s OR i.org_unit_id = ANY(%s))""", (interface_id, company_scope, scoped)).fetchone()
        if row is None:
            return _denied(request, "找不到这个部门界面，或你不在该部门范围内。")
        mode = _entry_mode_for(row["status"])
        latest = conn.execute(
            """SELECT * FROM oams.interface_entries
               WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
               ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
    return _interface_fill_page(request, row, latest=latest, saved=saved, error=error)


def _interface_fill_page(request: Request, row: dict[str, Any], *, latest: dict[str, Any] | None = None,
                         saved: str | None = None, error: str | None = None,
                         values: dict[str, Any] | None = None, status_code: int = 200):
    """Draw a department interface for filling.

    ``values`` overrides what the fields show, so a refused POST can come back with
    the text the person typed instead of the last saved entry or an empty form.
    """
    descriptor = row["descriptor"]
    fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
    if values is None:
        values = _interface_values(fields, latest)
    try:
        rendered = render_interface(descriptor, values=values)
    except RenderError as exc:
        return _render_failed(request, page=row, exc=exc, org_name=row.get("org_name"),
                              back_href="/interfaces", back_label="返回界面列表")
    return TEMPLATES.TemplateResponse(request, "interface_fill.html", {
        "interface": row, "rendered": rendered, "latest": latest,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "oams:interface-form:v1")}, status_code=status_code)


@router.post("/interfaces/{interface_id}")
async def interface_submit(request: Request, interface_id: UUID, csrf: str = Form(...)):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门界面需要 workforce.reports/read 与 submit 权限。")
    _check_form_token(request, csrf, "oams:interface-form:v1")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        row = conn.execute(
            """SELECT i.*, o.name AS org_name FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               WHERE i.interface_id = %s AND i.status = 'active'
                 AND (%s OR i.org_unit_id = ANY(%s))""", (interface_id, company_scope, scoped)).fetchone()
        if row is None:
            return _denied(request, "找不到这个部门界面，或你不在该部门范围内。")
        descriptor = row["descriptor"]
        fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
        form = await request.form()
        standard, extra, problem = _read_entry_form(fields, descriptor.get("status_map", {}), form)
        if problem is not None:
            # Say what is missing *on the filled-in form*: the browser's required
            # check is not there to help a hand-made POST, and a redirect would have
            # thrown away everything the person typed before we told them.
            mode = _entry_mode_for(row["status"])
            latest = conn.execute(
                """SELECT * FROM oams.interface_entries
                   WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
                   ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
            return _interface_fill_page(request, row, latest=latest, error=problem,
                                        values=_fill_values_from_form(fields, form),
                                        status_code=400)
        columns = dict(standard)
        columns["extra"] = Jsonb(extra)
        columns["reported_by"] = principal["member_id"]
        columns["entry_mode"] = _entry_mode_for(row["status"])
        keys = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        entry = conn.execute(
            "INSERT INTO oams.interface_entries (interface_id, " + keys + ") VALUES (%s, " + placeholders + ")"
            + " RETURNING entry_id, title, status, progress_value, evidence, reported_at",
            (interface_id, *columns.values())).fetchone()
        api.audit(conn, principal["member_id"], "interface_entry.created", "interface_entry",
                  entry["entry_id"], "submitted through department interface", entry)
    return RedirectResponse(url="/interfaces/" + str(interface_id) + "?saved=1", status_code=303)


@router.get("/setup/interfaces", response_class=HTMLResponse)
def setup_interfaces(request: Request, saved: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.work_items", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "建立部门界面需要 workforce.work_items/manage 权限。")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        org_units = conn.execute(
            """SELECT org_unit_id, org_code, name FROM oams.org_units WHERE status = 'active'
                 AND (%s OR org_unit_id = ANY(%s)) ORDER BY org_code""", (company_scope, scoped)).fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.name AS org_name
               FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
               WHERE p.status = 'active' AND (%s OR p.org_unit_id = ANY(%s))
               ORDER BY o.org_code, p.position_code""", (company_scope, scoped)).fetchall()
        existing = conn.execute(
            """SELECT i.interface_id, i.slug, i.name, i.status, i.visibility, i.version_no, o.name AS org_name
               FROM oams.interfaces i JOIN oams.org_units o USING (org_unit_id)
               WHERE (%s OR i.org_unit_id = ANY(%s)) ORDER BY o.name, i.name""",
            (company_scope, scoped)).fetchall()
    return TEMPLATES.TemplateResponse(request, "setup_interfaces.html", {
        "org_units": org_units, "positions": positions, "existing": existing,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "oams:interface-setup-form:v1")})

@router.post("/setup/interfaces")
def setup_create_interface(request: Request, csrf: str = Form(...), org_unit_id: UUID = Form(...),
                           slug: str = Form(...), name: str = Form(...),
                           position_id: str = Form(default=""), descriptor_text: str = Form(...)):
    try:
        principal = _require(request, "workforce.work_items", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "建立部门界面需要 workforce.work_items/manage 权限。")
    _check_form_token(request, csrf, "oams:interface-setup-form:v1")
    try:
        descriptor = json.loads(descriptor_text)
    except ValueError:
        return RedirectResponse(url="/setup/interfaces?error=json", status_code=303)
    if not isinstance(descriptor, dict):
        return RedirectResponse(url="/setup/interfaces?error=json", status_code=303)
    with connection() as conn:
        # The connection uses dict_row, so these come back keyed, not positional.
        codes = {row["org_code"] for row in conn.execute(
            "SELECT org_code FROM oams.org_units WHERE status = 'active'").fetchall()}
        positions_known = {row["position_code"] for row in conn.execute(
            "SELECT position_code FROM oams.positions WHERE status = 'active'").fetchall()}
        problems = validate_descriptor(descriptor, known_org_unit_codes=codes,
                                       known_position_codes=positions_known,
                                       # This is the one door that reaches a live
                                       # page without anyone signing it, so the
                                       # file has to carry the signature itself --
                                       # and the signature has to name an active
                                       # member, not a plausible-looking string.
                                       require_recorded_approval=True,
                                       known_employee_codes=_active_employee_codes(conn))
        if problems:
            return RedirectResponse(url="/setup/interfaces?error=" +
                                    urllib.parse.quote("；".join(problems)[:500]), status_code=303)
        require_org_unit_access(conn, principal["member_id"], org_unit_id)
        position = UUID(position_id) if position_id.strip() else None
        next_version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 AS next_version FROM oams.interfaces
               WHERE org_unit_id = %s AND slug = %s""",
            (org_unit_id, slug.strip())).fetchone()["next_version"]
        # Redrawing a screen must not leave two of them claiming to be current:
        # the previous live version is archived in the same transaction.
        conn.execute(
            """UPDATE oams.interfaces SET status = 'archived', updated_at = now()
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (org_unit_id, slug.strip()))
        # This door skips the review page, so the signature travels in the file --
        # and has to be written into the row as well, not just quoted in the JSON.
        # A public page without a reviewer on the row is exactly what the table's
        # own constraint forbids, and leaving it to the database would turn a
        # readable "your file's signer is unknown" into a 500.
        review = (descriptor.get("meta") or {}).get("review") or {}
        signer_code = review.get("reviewed_by")
        signer_id = (_member_id_for_code(conn, signer_code)
                     if isinstance(signer_code, str) and signer_code else None)
        row = conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    position_id, version_no, created_by, updated_by,
                    reviewed_by, reviewed_at)
               VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
               RETURNING interface_id, version_no""",
            (org_unit_id, slug.strip(), name.strip(),
             (descriptor.get("meta") or {}).get("visibility", "department"),
             descriptor.get("schema_version", "0.1"), Jsonb(descriptor), position, next_version,
             principal["member_id"], principal["member_id"],
             signer_id, review.get("reviewed_at") or None)).fetchone()
        api.audit(conn, principal["member_id"], "interface.created", "interface", row["interface_id"],
                  "created through setup page", {"slug": slug.strip(), "version_no": row["version_no"]})
    return RedirectResponse(url="/setup/interfaces?saved=1", status_code=303)

@router.get("/progress", response_class=HTMLResponse)
def progress_center(request: Request, stale_days: int = Query(default=7, ge=1, le=365)):
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/progress", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看进度中心需要工作项查看权限。")
    member_id = request.state.member["member_id"]
    with connection() as conn:
        level = admin_level(conn, member_id)
        everyone = conn.execute(
            """SELECT DISTINCT o.org_unit_id, o.name FROM oams.org_units o
               JOIN oams.work_items w USING (org_unit_id)
               WHERE o.status = 'active' ORDER BY o.name""").fetchall()
        # The company totals are the whole company for everybody; what changes by
        # role is the department detail underneath them. Building one board per
        # department is what /board does anyway, and there are only a handful.
        boards = [{"unit": unit, "board": api.build_board(conn, unit["org_unit_id"], stale_days)}
                  for unit in everyone]
        if level == ROLE_STAFF:
            mine = active_org_unit_ids(conn, member_id)
            shown = [entry for entry in boards if entry["unit"]["org_unit_id"] in mine]
        else:
            shown = boards
    totals = {key: sum(entry["board"]["summary"][key] for entry in boards)
              for key in ("open_items", "blocked", "needs_decision", "never_reported", "overdue")}
    return TEMPLATES.TemplateResponse(request, "progress.html", {
        "member": _member(request), "departments": shown, "totals": totals,
        "stale_days": stale_days, "status_labels": STATUS_LABELS,
        "channel_labels": CHANNEL_LABELS})


@router.get("/reports", response_class=HTMLResponse)
def department_reports(request: Request, org: UUID | None = None, saved: str | None = None,
                       error: str | None = None):
    try:
        principal = _require(request, "workforce.work_items", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/reports", status_code=302)
    except _NotPermitted:
        return _denied(request, "汇报需要工作项查看和进度提交权限。")
    with connection() as conn:
        assignments = conn.execute(
            """SELECT DISTINCT p.position_id, p.title, p.org_unit_id, o.name AS org_name
               FROM oams.position_assignments a
               JOIN oams.positions p USING (position_id)
               JOIN oams.org_units o ON o.org_unit_id = p.org_unit_id
               WHERE a.member_id = %s AND a.status IN ('active', 'scheduled')
                 AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
                 AND p.status = 'active' ORDER BY o.name, p.title""",
            (principal["member_id"],)).fetchall()
        allowed = {row["position_id"] for row in assignments}
        items = conn.execute(
            """SELECT w.work_item_id, w.org_unit_id, w.position_id, w.title, w.status,
                      o.name AS org_name, p.title AS position_title, r.progress_value,
                      r.note, r.evidence, r.submitted_via, r.reported_at
               FROM oams.work_items w JOIN oams.positions p USING (position_id)
               JOIN oams.org_units o ON o.org_unit_id = w.org_unit_id
               LEFT JOIN LATERAL (
                   SELECT progress_value, note, evidence, submitted_via, reported_at
                   FROM oams.work_item_reports WHERE work_item_id = w.work_item_id
                     AND status = 'active' ORDER BY reported_at DESC, report_id DESC LIMIT 1
               ) r ON true
               WHERE w.status IN ('planned', 'in_progress', 'blocked')
               ORDER BY o.name, p.title, w.updated_at DESC""").fetchall()
    # The reporting form shows only the member's active positions, even when
    # the member can manage work items elsewhere through the management API.
    visible = [row for row in items if row["position_id"] in allowed]
    departments = list(dict.fromkeys((row["org_unit_id"], row["org_name"]) for row in visible))
    if org and org not in {department_id for department_id, _ in departments}:
        return _denied(request, "当前没有这个部门的汇报事项。")
    selected = org or (departments[0][0] if departments else None)
    return TEMPLATES.TemplateResponse(request, "reports.html", {
        "member": _member(request), "departments": departments, "selected": selected,
        "items": [row for row in visible if row["org_unit_id"] == selected],
        "status_labels": STATUS_LABELS, "channel_labels": CHANNEL_LABELS,
        "saved": saved, "error": error, "csrf": _report_csrf(request)})


@router.post("/reports/{work_item_id}")
def submit_department_report(request: Request, work_item_id: UUID,
                             csrf: str = Form(...), progress_value: str = Form(""),
                             note: str = Form(""), evidence: str = Form(...)):
    try:
        principal = _require(request, "workforce.work_items", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/reports", status_code=302)
    except _NotPermitted:
        return _denied(request, "汇报需要工作项查看和进度提交权限。")
    if not hmac.compare_digest(csrf, _report_csrf(request)):
        raise HTTPException(status_code=403, detail="invalid report form token")
    with connection() as conn:
        item = conn.execute(
            """SELECT w.position_id, w.org_unit_id FROM oams.work_items w
               WHERE w.work_item_id = %s""", (work_item_id,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="work item not found")
        owns_position = conn.execute(
            """SELECT 1 FROM oams.position_assignments WHERE member_id = %s
                 AND position_id = %s AND status IN ('active', 'scheduled')
                 AND starts_at <= now() AND (ends_at IS NULL OR now() < ends_at)""",
            (principal["member_id"], item["position_id"])).fetchone()
        if not owns_position:
            return _denied(request, "只能汇报自己负责岗位的事项。")
    try:
        payload = ReportCreate(submitted_via="human", progress_value=int(progress_value) if progress_value.strip() else None,
                               note=note.strip() or None, evidence=evidence.strip())
    except (ValueError, ValidationError):
        return RedirectResponse(url=f"/reports?org={item['org_unit_id']}&error=invalid", status_code=303)
    try:
        api.create_report(work_item_id, payload, principal["member_id"])
    except HTTPException as exc:
        if exc.status_code in (409, 422):
            return RedirectResponse(url=f"/reports?org={item['org_unit_id']}&error=invalid", status_code=303)
        raise
    return RedirectResponse(url=f"/reports?org={item['org_unit_id']}&saved=1", status_code=303)


@router.get("/board", response_class=HTMLResponse)
def board(request: Request, org: UUID | None = None, stale_days: int = Query(default=7, ge=1, le=365),
          layer: str = Query(default="all"), stage: str | None = Query(default=None)):
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看看板需要 workforce.work_items/read 权限。")
    layer = api.normalize_layer(layer)
    stage = api.normalize_stage(stage)

    with connection() as conn:
        if has_company_scope(conn, request.state.member["member_id"]):
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
                   WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        else:
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
                   WHERE status = 'active' AND org_unit_id = ANY(%s)
                   ORDER BY unit_type DESC, org_code""",
                (list(active_org_unit_ids(conn, request.state.member["member_id"])),)).fetchall()
        if not org_units:
            return TEMPLATES.TemplateResponse(request, "board.html",
                                              {"org_units": [], "board": None, "member": _member(request)})
        selected_id = org or org_units[0]["org_unit_id"]
        require_org_unit_access(conn, request.state.member["member_id"], selected_id)
        data = api.build_board(conn, selected_id, stale_days, layer, stage)
    return TEMPLATES.TemplateResponse(request, "board.html", {
        "org_units": org_units,
        "selected_id": str(selected_id),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "stage": stage,
        "member": _member(request),
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


@router.get("/board/body", response_class=HTMLResponse)
def board_body(request: Request, org: UUID, stale_days: int = Query(default=7, ge=1, le=365),
               layer: str = Query(default="all"), stage: str | None = Query(default=None)):
    """The board content alone, so switching department or layer does not reload the page."""
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看看板需要 workforce.work_items/read 权限。")
    layer = api.normalize_layer(layer)
    stage = api.normalize_stage(stage)
    with connection() as conn:
        require_org_unit_access(conn, request.state.member["member_id"], org)
        data = api.build_board(conn, org, stale_days, layer, stage)
        if has_company_scope(conn, request.state.member["member_id"]):
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
                   WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        else:
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
                   WHERE status = 'active' AND org_unit_id = ANY(%s)
                   ORDER BY unit_type DESC, org_code""",
                (list(active_org_unit_ids(conn, request.state.member["member_id"])),)).fetchall()
    return TEMPLATES.TemplateResponse(request, "board_body.html", {
        "org_units": org_units,
        "selected_id": str(org),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "stage": stage,
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


PILOT_REPORTING_GRANTS = (
    ("workforce.org_units", "read"),
    ("workforce.positions", "read"),
    ("workforce.work_items", "read"),
    ("workforce.reports", "read"),
    ("workforce.reports", "submit"),
)


def _require_org_access(principal: dict, org_unit_id: Any) -> None:
    with connection() as conn:
        require_org_unit_access(conn, principal["member_id"], org_unit_id)


@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request, saved: str | None = None, error: str | None = None,
          member_id: str | None = None, login: str | None = None, expires: str | None = None):
    """Guided pilot setup: department, position, member, assignment, grants, login link."""
    try:
        principal = _require(request, "workforce.grants", "manage")
        _require(request, "workforce.members", "manage")
        _require(request, "workforce.positions", "manage")
        _require(request, "workforce.assignments", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    except _NotPermitted:
        return _denied(request, "试用设置需要组织、成员、岗位、任职和授权管理权限。")
    with connection() as conn:
        org_units = conn.execute(
            """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
               WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.name AS org_name
               FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
               WHERE p.status = 'active' ORDER BY o.org_code, p.position_code""").fetchall()
        members = conn.execute(
            """SELECT member_id, employee_code, display_name, employment_status
               FROM oams.workforce_members ORDER BY display_name""").fetchall()
    return TEMPLATES.TemplateResponse(request, "setup.html", {
        "org_units": org_units, "positions": positions, "members": members,
        "saved": saved, "error": error, "created_member_id": member_id,
        "login_token": login, "login_expires": expires, "member": _member(request),
        "csrf_org": _form_token(request, "oams:org-form:v1"),
        "csrf_position": _form_token(request, "oams:position-form:v1"),
        "csrf_member": _form_token(request, "oams:member-form:v1"),
        "csrf_pilot": _form_token(request, "oams:pilot-form:v1"),
    })


@router.post("/setup/org-units")
def setup_create_org_unit(request: Request, csrf: str = Form(...), org_code: str = Form(...),
                          name: str = Form(...), parent_org_unit_id: str = Form(default=""),
                          unit_type: str = Form("department")):
    try:
        principal = _require(request, "workforce.org_units", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    except _NotPermitted:
        return _denied(request, "新建部门需要 workforce.org_units/manage 权限。")
    _check_form_token(request, csrf, "oams:org-form:v1")
    try:
        payload = OrgUnitCreate(org_code=org_code.strip(), name=name.strip(),
                                parent_org_unit_id=UUID(parent_org_unit_id) if parent_org_unit_id.strip() else None,
                                unit_type=unit_type)
    except (ValidationError, ValueError):
        return RedirectResponse(url="/setup?error=org", status_code=303)
    try:
        with connection() as conn:
            api.insert_org_unit(conn, payload, principal["member_id"])
    except HTTPException as exc:
        if exc.status_code == 409:
            return RedirectResponse(url="/setup?error=org-duplicate", status_code=303)
        raise
    return RedirectResponse(url="/setup?saved=org", status_code=303)


@router.post("/setup/positions")
def setup_create_position(request: Request, csrf: str = Form(...), position_code: str = Form(...),
                          title: str = Form(...), org_unit_id: UUID = Form(...)):
    try:
        principal = _require(request, "workforce.positions", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    except _NotPermitted:
        return _denied(request, "新建岗位需要 workforce.positions/manage 权限。")
    _check_form_token(request, csrf, "oams:position-form:v1")
    _require_org_access(principal, org_unit_id)
    try:
        payload = PositionCreate(position_code=position_code.strip(), title=title.strip(), org_unit_id=org_unit_id)
    except ValidationError:
        return RedirectResponse(url="/setup?error=position", status_code=303)
    try:
        with connection() as conn:
            api.insert_position(conn, payload, principal["member_id"])
    except UniqueViolation:
        return RedirectResponse(url="/setup?error=position-duplicate", status_code=303)
    return RedirectResponse(url="/setup?saved=position", status_code=303)


@router.post("/setup/pilot")
def setup_pilot_member(request: Request, csrf: str = Form(...), display_name: str = Form(...),
                       employee_code: str = Form(default=""), position_id: UUID = Form(...)):
    """Create an active member, seat them on one position, grant reporting, hand back a login link."""
    try:
        principal = _require(request, "workforce.members", "manage")
        _require(request, "workforce.assignments", "manage")
        _require(request, "workforce.grants", "manage")
        _require(request, "workforce.positions", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    except _NotPermitted:
        return _denied(request, "建立试用成员需要成员、任职和授权管理权限。")
    _check_form_token(request, csrf, "oams:pilot-form:v1")
    now = datetime.now(timezone.utc).isoformat()
    try:
        member_payload = MemberCreate(employee_code=employee_code.strip() or None, display_name=display_name.strip())
    except ValidationError:
        return RedirectResponse(url="/setup?error=pilot", status_code=303)
    try:
        with connection() as conn:
            created = api.insert_member(conn, member_payload, principal["member_id"])
            member_id = created["member_id"]
            for resource, action in PILOT_REPORTING_GRANTS:
                api.insert_grant(conn, GrantCreate(
                    subject_type="position", position_id=position_id, resource=resource, action=action,
                    scope_type="all", valid_from=now, reason="pilot reporting access from guided setup page"),
                    principal["member_id"])
            api.insert_assignment(conn, AssignmentCreate(
                member_id=member_id, position_id=position_id, assignment_type="primary", starts_at=now,
                reason="pilot setup from guided setup page"), principal["member_id"])
        token, expires_at = issue_session(str(member_id), "pilot-setup")
    except HTTPException as exc:
        if exc.status_code == 409:
            return RedirectResponse(url="/setup?error=pilot-duplicate", status_code=303)
        raise
    return RedirectResponse(url="/setup?saved=pilot&member_id=" + str(member_id)
                            + "&login=" + urllib.parse.quote(token)
                            + "&expires=" + urllib.parse.quote(expires_at.isoformat()), status_code=303)


@router.get("/admin/work-items", response_class=HTMLResponse)
def admin_work_items(request: Request, created: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/work-items", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看工作项需要 workforce.work_items/read 权限。")
    with connection() as conn:
        can_manage = has_permission(conn, principal["member_id"], "workforce.work_items", "manage")
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped_orgs = list(active_org_unit_ids(conn, principal["member_id"]))
        scope_sql = "" if company_scope else "WHERE w.org_unit_id = ANY(%s)"
        scope_params = [] if company_scope else [scoped_orgs]
        items = conn.execute(
            f"""SELECT {api.WORK_ITEM_SELECT}
                  FROM oams.work_items w
                  JOIN oams.positions p USING (position_id)
                  JOIN oams.org_units o ON o.org_unit_id = w.org_unit_id
                  {scope_sql}
                 ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                                        WHEN 'planned' THEN 2 ELSE 3 END, w.updated_at DESC""", scope_params).fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.org_code
               FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
               WHERE p.status = 'active' AND (%s OR p.org_unit_id = ANY(%s))
               ORDER BY o.org_code, p.position_code""", (company_scope, scoped_orgs)).fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_work_items.html", {
        "items": items, "positions": positions, "can_manage": can_manage,
        "created": created, "error": error, "member": _member(request),
        "status_labels": STATUS_LABELS, "kind_labels": KIND_LABELS,
        "csrf": _form_token(request, "oams:work-item-form:v1"),
    })


@router.post("/admin/work-items")
def admin_create_work_item(request: Request,
                           csrf: str = Form(...),
                           position_id: UUID = Form(...),
                           title: str = Form(...),
                           kind: str = Form("routine"),
                           status: str = Form("planned"),
                           blocked_reason: str = Form(""),
                           needs_decision: bool = Form(False)):
    try:
        principal = _require(request, "workforce.work_items", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/work-items", status_code=302)
    except _NotPermitted:
        return _denied(request, "新建工作项需要 workforce.work_items/manage 权限。")
    _check_form_token(request, csrf, "oams:work-item-form:v1")
    try:
        payload = WorkItemCreate(
            position_id=position_id, title=title.strip(), kind=kind, status=status,
            blocked_reason=blocked_reason.strip() or None, needs_decision=needs_decision)
    except ValidationError:
        # "blocked" without a reason is the one people will trip over; send them
        # back with the message instead of a stack trace.
        return RedirectResponse(url="/admin/work-items?error=blocked", status_code=303)
    try:
        with connection() as conn:
            position = conn.execute("SELECT org_unit_id FROM oams.positions WHERE position_id = %s",
                                    (payload.position_id,)).fetchone()
            if position:
                require_org_unit_access(conn, principal["member_id"], position["org_unit_id"])
            api.insert_work_item(conn, payload, principal["member_id"])
    except CheckViolation:
        return RedirectResponse(url="/admin/work-items?error=blocked", status_code=303)
    return RedirectResponse(url="/admin/work-items?created=1", status_code=303)


@router.get("/admin/positions", response_class=HTMLResponse)
def admin_positions(request: Request, created: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.positions", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/positions", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看岗位需要 workforce.positions/read 权限。")
    with connection() as conn:
        can_manage = has_permission(conn, principal["member_id"], "workforce.positions", "manage")
        rows = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, p.status, o.org_code, o.name AS org_name,
                      (SELECT count(*) FROM oams.position_assignments a
                        WHERE a.position_id = p.position_id AND a.status IN ('active', 'scheduled')
                          AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)) AS holder_count
               FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
               ORDER BY o.org_code, p.position_code""").fetchall()
        org_units = conn.execute(
            "SELECT org_unit_id, org_code, name FROM oams.org_units WHERE status = 'active' ORDER BY org_code"
        ).fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_positions.html", {
        "positions": rows, "org_units": org_units, "can_manage": can_manage,
        "created": created, "error": error, "member": _member(request),
        "csrf": _form_token(request, "oams:position-form:v1")})


@router.post("/admin/positions")
def admin_create_position(request: Request,
                          csrf: str = Form(...),
                          position_code: str = Form(...),
                          title: str = Form(...),
                          org_unit_id: UUID = Form(...)):
    try:
        principal = _require(request, "workforce.positions", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/positions", status_code=302)
    except _NotPermitted:
        return _denied(request, "新建岗位需要 workforce.positions/manage 权限。")
    _check_form_token(request, csrf, "oams:position-form:v1")
    payload = PositionCreate(position_code=position_code.strip(), title=title.strip(), org_unit_id=org_unit_id)
    try:
        with connection() as conn:
            api.insert_position(conn, payload, principal["member_id"])
    except UniqueViolation:
        return RedirectResponse(url="/admin/positions?error=duplicate", status_code=303)
    except ForeignKeyViolation:
        return RedirectResponse(url="/admin/positions?error=org", status_code=303)
    return RedirectResponse(url="/admin/positions?created=1", status_code=303)


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    try:
        _require(request, "workforce.audit", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/audit", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看审计需要 workforce.audit/read 权限。")
    with connection() as conn:
        events = conn.execute(
            """SELECT e.event_id, e.actor_member_id, m.display_name AS actor_name, e.event_type,
                      e.subject_type, e.subject_id, e.reason, e.occurred_at
               FROM oams.workforce_audit_events e
               LEFT JOIN oams.workforce_members m ON m.member_id = e.actor_member_id
               ORDER BY e.occurred_at DESC LIMIT %s""", (limit,)).fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_audit.html",
                                      {"events": events, "limit": limit, "member": _member(request)})


@router.get("/settings", response_class=HTMLResponse)
def settings_home(request: Request):
    """Who exists, what each job can reach, what changed lately.

    One screen answers the three questions a manager actually asks about the
    setup, in that order. The step-by-step wizard -- create a department, create
    a job, create the first person -- keeps its own page at /setup: it is a
    one-off flow, and mixing a wizard into an overview makes both harder to read.
    """
    try:
        _require(request, "workforce.grants", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/settings", status_code=302)
    except _NotPermitted:
        return _denied(request, "设置页只有管理员可以打开。")
    with connection() as conn:
        org_units = conn.execute(
            """SELECT u.org_unit_id, u.name, u.org_code, u.unit_type,
                      parent.name AS parent_name,
                      (SELECT count(*)
                         FROM oams.position_assignments a
                         JOIN oams.positions held ON held.position_id = a.position_id
                        WHERE held.org_unit_id = u.org_unit_id
                          AND a.status = 'active') AS member_count,
                      (SELECT count(*)
                         FROM oams.interfaces i
                        WHERE i.org_unit_id = u.org_unit_id
                          AND i.status = 'active') AS page_count
               FROM oams.org_units u
               LEFT JOIN oams.org_units parent ON parent.org_unit_id = u.parent_org_unit_id
               WHERE u.status = 'active'
               ORDER BY u.name""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.title, p.position_code, o.name AS org_name
               FROM oams.positions p
               JOIN oams.org_units o ON o.org_unit_id = p.org_unit_id
               WHERE p.status = 'active'
               ORDER BY o.name, p.title""").fetchall()
        grants = conn.execute(
            """SELECT position_id, resource, action
               FROM oams.permission_grants
               WHERE subject_type = 'position' AND revoked_at IS NULL
               ORDER BY resource, action""").fetchall()
        events = conn.execute(
            """SELECT e.event_type, e.subject_type, e.reason, e.occurred_at,
                      m.display_name AS actor_name
               FROM oams.workforce_audit_events e
               LEFT JOIN oams.workforce_members m ON m.member_id = e.actor_member_id
               ORDER BY e.occurred_at DESC, e.event_id DESC
               LIMIT 5""").fetchall()

    by_position: dict = {}
    for grant in grants:
        by_position.setdefault(grant["position_id"], []).append(grant)
    position_rows = [
        dict(row, capabilities=admin_labels.summarize_capabilities(by_position.get(row["position_id"], [])))
        for row in positions
    ]
    recent = [dict(row, action=admin_labels.describe_event(row["event_type"])) for row in events]
    return TEMPLATES.TemplateResponse(request, "settings.html", {
        "member": _member(request),
        "org_units": org_units,
        "positions": position_rows,
        "recent": recent,
    })


# --------------------------------------------------------------------------- #
# 协作室：右下角按钮呼出的全局讨论层
#
# 三条规则（谁能看见哪个群、什么算未读、消息只能发不能改）都写在 collab.py，
# 这一层只负责把数据端到界面上。所以这里没有一处自己判断可见性——一律走
# collab.can_see / collab.visible_*，页面的答案和将来 MCP 工具的答案才会是同一个。
#
# 形态是「底部两栏弹层」：从窗口底边升起，上两角 26（--r-card）、下两角 0，左栏会话列表
# 264 定宽，右栏对话自适应。它不是导航项，也不和当前停在哪个页面有关——
# 左栏列的就是全部会话。
# --------------------------------------------------------------------------- #

#: 一次取多少条消息。这是分页参数，不是设计值：界面只承诺「加载行不占位」。
COLLAB_PAGE_LIMIT = 30

COLLAB_PLACEHOLDER = {
    collab.KIND_MANAGE: "对这个部门发言…",
    collab.KIND_FILL: "对这页的填报口径发言…",
}

#: 发言表单的 CSRF 用途串。跟其它表单一样，令牌绑在会话 cookie 上。
COLLAB_FORM_PURPOSE = "oams:collab-form:v1"


def _collab_columns(conn, member_id: Any, q: str | None = None) -> dict[str, Any]:
    """左栏的三段会话 + 总未读。

    q 只按会话名过滤。设计上搜索最终要同时搜消息与同事（那是独立检索面），
    这一版先只做到「左栏能筛」，所以占位文案也只写「搜索会话」——说了能搜
    消息却搜不到，比没有这个框更糟。
    """
    objects = collab.visible_objects(conn, member_id)
    dms = collab.visible_dms(conn, member_id)
    needle = (q or "").strip().lower()
    if needle:
        objects = [row for row in objects if needle in row["title"].lower()]
        dms = [row for row in dms if needle in row["title"].lower()]
    for row in objects + dms:
        row["is_dm"] = row["kind"] == collab.KIND_DM
        row["avatar"] = (row["title"] or "群")[:1]
        if row["thread_id"]:
            row["href"] = "/collab/threads/" + str(row["thread_id"])
        else:
            params = ["kind=" + row["kind"]]
            if row["org_unit_id"]:
                params.append("org_unit_id=" + str(row["org_unit_id"]))
            if row["interface_id"]:
                params.append("interface_id=" + str(row["interface_id"]))
            row["href"] = "/collab/open?" + "&".join(params)
    groups = []
    for kind in collab.KIND_ORDER:
        rows = dms if kind == collab.KIND_DM else [r for r in objects if r["kind"] == kind]
        if rows:
            groups.append({"kind": kind, "label": collab.KIND_LABELS[kind], "rows": rows})
    return {"groups": groups, "unread_total": collab.unread_total(conn, member_id),
            "q": q or ""}



def _collab_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把消息切成「日期分隔 + 消息」的流水。

    日期分隔只在跨天处插一条，同一天连续消息之间不插。切在服务端而不是模板里，
    是因为模板一旦开始判断日期，分页拼接上来的那一段就没有上下文可比了。
    """
    items: list[dict[str, Any]] = []
    last_day = None
    for message in messages:
        day = _display_time(message["created_at"], "%Y-%m-%d")
        if day != last_day:
            items.append({"type": "day",
                          "label": _display_time(message["created_at"], "%m月%d日")})
            last_day = day
        items.append({"type": "msg", "msg": message})
    return items


def _collab_thread_payload(conn, member_id: Any, thread: dict[str, Any], *,
                           before_seq: int | None = None,
                           error: str | None = None,
                           draft: str = "",
                           csrf: str = "") -> dict[str, Any]:
    """右栏要的全部东西。

    before_seq 是「加载更早」的游标：取到它之前的那一批，多取一条用来判断还有
    没有更早的。多取的那条是这批里最旧的一条，看完就丢——它已经在更早的那一
    屏里出现过了。
    """
    messages = collab.list_messages(conn, thread["thread_id"], before_seq=before_seq,
                                    limit=COLLAB_PAGE_LIMIT + 1)
    has_more = len(messages) > COLLAB_PAGE_LIMIT
    if has_more:
        messages = messages[1:]
    return {
        "header": collab.thread_header(conn, member_id, thread),
        "thread": thread,
        "items": _collab_items(messages),
        "has_more": has_more,
        "earlier_cursor": messages[0]["seq"] if (has_more and messages) else None,
        "placeholder": COLLAB_PLACEHOLDER.get(thread["kind"], "发言…"),
        "error": error,
        "draft": draft,
        "csrf": csrf,
    }


def _collab_error(request: Request, member: dict[str, Any], message: str,
                  status_code: int = 404):
    """会话打不开时：右栏说一句实话，左栏照常可用。

    传输层给 404，不给 403 也不给 200：403 等于告诉外面「这个会话是存在的」，
    给枚举留了一道缝；200 又让浏览器和外部工具以为这是一张打开了的正常页面。
    文案本来就同时盖住「不存在」和「不开放给你」，所以两种原因共用一个码。
    `/collab/threads/{id}/messages` 那个数据片段仍然回 403——它是接口不是页面。
    """
    with connection() as conn:
        data = _collab_columns(conn, member["member_id"])
    return TEMPLATES.TemplateResponse(request, "collab/error.html",
                                      {"member": member, "message": message, **data},
                                      status_code=status_code)

@router.get("/collab", response_class=HTMLResponse)
def collab_panel(request: Request):
    """打开协作室。不标已读——面板只是打开，还没点进任何会话。"""
    member = _member(request)
    if member is None:
        return RedirectResponse(url="/login", status_code=302)
    with connection() as conn:
        data = _collab_columns(conn, member["member_id"])
        directory = collab.directory(conn, member["member_id"],
                                     limit=collab.DIRECTORY_CHUNK)
    return TEMPLATES.TemplateResponse(request, "collab/panel.html",
                                      {"member": member, "directory": directory,
                                       "total_admins": directory["total_admins"],
                                       "total_peers": directory["total_peers"], **data})


def _collab_side(conn, member_id: Any, view: str,
                 q: str | None = None, offset: int = 0,
                 after: str | None = None,
                 limit: int | None = None) -> dict[str, Any]:
    """左栏那一个容器的两种内容：会话列表 / 通讯录。

    view 由界面上的 data-collab-view 决定，服务端不记这个状态：左栏只有一个
    容器，谁最后写谁说了算，多存一份服务端状态就多一处会和 DOM 对不上的地方。

    通讯录按 window 发：首包、搜索、滚到底都走这里，`offset` 是已经发下去的行数，
    `limit` 只让「点开一个私聊之后左栏重取」把已经滚出来的行原样重发一遍——
    不传的话那一重取会把列表缩回首包那一窗，人滚到一半的名单全没了。
    """
    directory = view == "directory"
    if directory:
        data = collab.directory(conn, member_id, q, offset=max(offset, 0),
                                limit=limit or collab.DIRECTORY_CHUNK,
                                after=after or None)
        # 会话分支的 `_collab_columns` 已经把未读一起算回来了，这里只补通讯录这一半：
        # 一次渲染只准问一次库，角标和列表不许各算一遍。
        data["unread_total"] = collab.unread_total(conn, member_id)
    else:
        data = _collab_columns(conn, member_id, q)
    data["view"] = "directory" if directory else "threads"
    return data


@router.get("/collab/side", response_class=HTMLResponse)
def collab_side(request: Request, view: str = "threads", q: str | None = None,
                offset: int = 0, after: str | None = None,
                limit: int | None = None):
    """左栏。搜索框打一个字、切一次视图、通讯录滚到底，都是换这一段。"""
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    with connection() as conn:
        data = _collab_side(conn, member["member_id"], view, q, offset=offset,
                            after=after, limit=min(limit, 200) if limit else None)
    return TEMPLATES.TemplateResponse(request, "collab/side.html",
                                      {"member": member, **data})


@router.get("/collab/threads", response_class=HTMLResponse)
def collab_threads(request: Request, q: str | None = None):
    """左栏会话列表（只要会话那一版）。留着是给测试与文档里的旧链接用。"""
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    with connection() as conn:
        data = _collab_columns(conn, member["member_id"], q)
    return TEMPLATES.TemplateResponse(request, "collab/thread_list.html",
                                      {"member": member, **data})


@router.get("/collab/open", response_class=HTMLResponse)
def collab_open(request: Request, kind: str,
                org_unit_id: UUID | None = None,
                interface_id: UUID | None = None,
                peer_id: UUID | None = None):
    """点一个「还没有人说过话」的对象：这一刻才建线程。

    会话不预建，所以左栏列的是对象；点进来才 INSERT。并发点两次撞上部分唯一
    索引是正常结果，ensure_thread 用 ON CONFLICT DO NOTHING 吃掉它。
    """
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    member_id = member["member_id"]
    with connection() as conn:
        try:
            thread = collab.ensure_thread(conn, member_id, kind=kind, org_unit_id=org_unit_id,
                                          interface_id=interface_id, peer_id=peer_id)
        except collab.CollabError as exc:
            return _collab_error(request, member, str(exc))
        collab.mark_read(conn, thread["thread_id"], member_id)
        payload = _collab_thread_payload(conn, member_id, thread,
                                         csrf=_form_token(request, COLLAB_FORM_PURPOSE))
        payload.update(_collab_columns(conn, member_id))
    return TEMPLATES.TemplateResponse(request, "collab/thread.html",
                                      {"member": member, **payload})


@router.get("/collab/threads/{thread_id}", response_class=HTMLResponse)
def collab_thread(request: Request, thread_id: UUID):
    """打开一个会话：读到底、把它标成已读、回右栏整屏。"""
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    member_id = member["member_id"]
    with connection() as conn:
        thread = collab.get_thread(conn, thread_id)
        if thread is None or thread["status"] != "active" or not collab.can_see(conn, member_id, thread):
            return _collab_error(request, member, "这个会话不存在，或者不开放给你。")
        collab.mark_read(conn, thread_id, member_id)
        payload = _collab_thread_payload(conn, member_id, thread,
                                         csrf=_form_token(request, COLLAB_FORM_PURPOSE))
        payload.update(_collab_columns(conn, member_id))
    return TEMPLATES.TemplateResponse(request, "collab/thread.html",
                                      {"member": member, **payload})


@router.get("/collab/threads/{thread_id}/messages", response_class=HTMLResponse)
def collab_earlier(request: Request, thread_id: UUID, before_seq: int):
    """「加载更早」那一批：只回更早的消息，由前端插到列表最前面。

    只回这一段而不是整屏，是因为整屏替换会把滚动位置和自己刚发的那句一起弄丢。
    """
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    member_id = member["member_id"]
    with connection() as conn:
        thread = collab.get_thread(conn, thread_id)
        if thread is None or thread["status"] != "active" or not collab.can_see(conn, member_id, thread):
            return HTMLResponse("", status_code=403)
        messages = collab.list_messages(conn, thread_id, before_seq=before_seq,
                                        limit=COLLAB_PAGE_LIMIT + 1)
        has_more = len(messages) > COLLAB_PAGE_LIMIT
        if has_more:
            messages = messages[1:]
    return TEMPLATES.TemplateResponse(request, "collab/earlier.html", {
        "member": member, "thread": thread, "items": _collab_items(messages),
        "has_more": has_more,
        "earlier_cursor": messages[0]["seq"] if (has_more and messages) else None,
    })


@router.post("/collab/threads/{thread_id}/messages", response_class=HTMLResponse)
def collab_post_message(request: Request, thread_id: UUID, body: str = Form(""),
                        csrf: str = Form(""), client_msg_id: str = Form("")):
    """发一条。失败时把正文原样退回输入框，输入框上方给一句原因。

    表单令牌跟其它写入表单同一套：绑在会话 cookie 上，转发别人的链接发不出去。

    `client_msg_id` 是断网重发的幂等键，由界面生成：同一条消息重发复用同一个 id，
    后端只落一条、第二遍返回已有那条。空串当没带——否则所有「没带 id」的消息
    会撞上 `UNIQUE (thread_id, client_msg_id)`。
    """
    member = _member(request)
    if member is None:
        return HTMLResponse("", status_code=401)
    _check_form_token(request, csrf, COLLAB_FORM_PURPOSE)
    member_id = member["member_id"]
    idempotency_key = (client_msg_id or "").strip()[:100] or None
    with connection() as conn:
        thread = collab.get_thread(conn, thread_id)
        if thread is None or thread["status"] != "active" or not collab.can_see(conn, member_id, thread):
            return _collab_error(request, member, "这个会话不存在，或者不开放给你。")
        error = None
        try:
            collab.post_message(conn, thread, member_id, body,
                                client_msg_id=idempotency_key)
        except collab.CollabError as exc:
            error = str(exc)
        collab.mark_read(conn, thread_id, member_id)
        payload = _collab_thread_payload(conn, member_id, thread, error=error,
                                         draft="" if error is None else body,
                                         csrf=_form_token(request, COLLAB_FORM_PURPOSE))
        payload.update(_collab_columns(conn, member_id))
    return TEMPLATES.TemplateResponse(request, "collab/thread.html",
                                      {"member": member, **payload})


@router.get("/collab/unread.json")
def collab_unread(request: Request):
    """入口按钮上那个数。没有实时推送：进页面算一次，开面板再算一次。"""
    member = _member(request)
    if member is None:
        return JSONResponse({"total": 0})
    with connection() as conn:
        return JSONResponse({"total": collab.unread_total(conn, member["member_id"])})

