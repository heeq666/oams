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
import html as html_text
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from oams import admin_labels, api, collab, metrics, review as review_rules, security
from oams.config import settings
from oams.db import connection
from oams.descriptor import recorded_approval_gaps, validate_descriptor
from oams.render import RenderError, render_interface, render_preview, render_read_only
from oams.schemas import (AssignmentCreate, GrantCreate, MemberCreate, OrgUnitCreate, PositionCreate,
                             ReportCreate, WorkItemCreate)
from oams.security import (DEV_WORKER_COOKIE, ROLE_DEPARTMENT, ROLE_STAFF, ROLE_SYSTEM,
                              accessible_org_unit_ids, active_org_unit_ids, admin_level,
                              administered_org_unit_ids,
                              can_access_org_unit,
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

# The states the concept doc requires to be visible on every entry (ch.6). The SQL
# that decides them lives in api.DEPARTMENT_ROLLUP_STATE_SQL (department page) and
# api.ROLLUP_STATE_SQL (company number), so the badge, the board panel and the JSON
# endpoint cannot disagree about which entries counted.
#
# Two of these say almost the same thing on purpose. `counted` counts on this page
# and would also reach the company; `counted_unlinked` counts on this page but has
# no work item yet, so it is not in the company number. Saying "未关联，不算数" for
# the second one was the old wording and it was wrong: the department's own number
# did count it.
ROLLUP_STATE_LABELS = {
    "counted": "已计入本部门，也进公司汇总",
    "counted_unlinked": "已计入本部门，未进公司汇总",
    "rolled": "已进汇总",
    "unlinked": "未关联，不进汇总",
    "draft": "草稿，不进汇总",
    "retracted": "已撤回",
}
# Badge classes that already exist in base.html; no new stylesheet rule needed.
ROLLUP_STATE_BADGES = {"counted": "human", "counted_unlinked": "vacant",
                       "rolled": "human", "unlinked": "vacant", "draft": "decision",
                       "retracted": ""}
ENTRY_LINK_PURPOSE = "oams:entry-link:v1"


def _with_rollup_labels(rows):
    """Give each entry the short Chinese label and badge class its state deserves."""
    rows = list(rows or [])
    marked = []
    for row in rows:
        item = dict(row)
        state = item.get("rollup_state") or "counted_unlinked"
        item["rollup_state"] = state
        item["rollup_label"] = ROLLUP_STATE_LABELS.get(state, state)
        item["rollup_badge"] = ROLLUP_STATE_BADGES.get(state, "")
        item["rollup_blocker"] = _rollup_gap(state)
        marked.append(item)
    return marked


def _rollup_gap(state):
    """"Why is this one not in the company number", in the department's words.

    The concept doc asks the UI to name the missing condition instead of only
    marking the entry, so a counted-but-unlinked entry says what would change its
    mind without pretending the department does not count it.
    """
    return {
        "counted_unlinked": "本部门算数；关联工作项后才会进公司汇总",
        "unlinked": "还没关联工作项：关联后这条才会进公司汇总",
        "draft": "试填数据：本部门的数也不计它；正式提交后才算",
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
def home(request: Request):
    # 首页落地的是「这个人第一眼该看的屏」——进度中心优先，看板不是首页。
    return RedirectResponse(url=_landing_url(request), status_code=302)


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
        elif map_to == "status":
            # 状态下拉框里的选项是本部门自己的词（「待取」），而 `status` 列存的是映射
            # 后的标准态（planned）——直接回填标准态永远选不中，任何已存条目的下拉都
            # 空着。写入时把部门词原样留一份在 extra[key]，这里优先取它；没留（本次
            # 改动之前记的条目）就退回标准态，跟以前一样回不出词，但不猜：一个标准态
            # 可能对上多个部门词，反推会把「待取」显示成「已取」。
            word = (entry["extra"] or {}).get(key)
            values[key] = word if word else entry["status"]
        elif map_to in ("title", "blocked_reason", "progress_value", "note", "evidence"):
            values[key] = entry[map_to]
        else:
            # 有标准含义、但表里没有对应列的字段（工单号、承诺完成日、照片链接…）写库时
            # 一律落在 `extra[字段key]`（见 _read_entry_form 最后一段）。这里以前只认标准列，
            # 于是这些字段在页面上永远是空的：填了「工单号 IT-2026-1102-01」，整页一次都不
            # 出现，只有查件框能从 extra 查回来。值在哪儿取，写入与回填必须是同一套。
            values[key] = (entry["extra"] or {}).get(key)
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
            # 部门词原样也留一份：`status` 列存标准态（汇总与四态靠它），而回填下拉框
            # 需要的是「待取」这种本部门自己的词，标准态对不上任何选项。见
            # _interface_values 里同一段说明。键用字段 key，与 display_only 的存法一致。
            extra[key] = raw
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
            continue
        # 剩下的都是「有标准含义、但表里没有对应列」的字段（金额、完成日期、计划
        # 日期、款号…）。它们曾经在这里被悄悄丢掉——填了也不存，页面上看得见填
        # 过、库里查不到，指标自然永远算不出来。落进 extra 就不会丢；能不能参与
        # 汇总由 api.ROLLUP_COLUMNS / EXTRA_AGGREGATE_COLUMNS 决定，不在这里判。
        extra[key] = raw
    if standard.get("status") == "blocked" and not (standard.get("blocked_reason") or "").strip():
        return {}, {}, "标记为阻塞时必须写明卡在哪、在等谁"
    return standard, extra, None


def _visible_departments(conn, member_id):
    """Departments this member may work in: their own, or all for company scope."""
    if has_company_scope(conn, member_id):
        return conn.execute(
            """SELECT org_unit_id, org_code, name FROM oams.org_units
               WHERE status = 'active' AND unit_type <> 'company' ORDER BY name""").fetchall()
    scoped = list(accessible_org_unit_ids(conn, member_id))
    if not scoped:
        return []
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM oams.org_units
           WHERE status = 'active' AND org_unit_id = ANY(%s) ORDER BY name""", (scoped,)).fetchall()


def _department_pages(conn, org_unit_id):
    """Every page of a department, drafts included: a department must see what it is building.

    ``slug`` rides along because a page is addressed by ``(org_code, slug)``, not by
    its version row (contract 6.4): anything that links to a page from here must be
    able to build ``/pages/{org_code}/{slug}`` instead of a link that rots at the
    next publish.
    """
    return conn.execute(
        """SELECT i.interface_id, i.slug, i.name, i.status, i.version_no, i.visibility,
                  (SELECT count(*) FROM oams.interface_entries e
                    WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
           FROM oams.interfaces i
           WHERE i.org_unit_id = %s AND i.status <> 'archived'
           ORDER BY (i.status = 'active') DESC, i.name""", (org_unit_id,)).fetchall()


def _resolve_department(conn, principal, org_unit_id):
    """Return the department row, or None when this member has no business there.

    范围只有一个判据：`security.can_access_org_unit`（岗位所在的部门 ∪ 被授权管的
    那几个部门；全公司范围等于全部）。建页与看页都走这里，别在路由里另写一套。
    """
    if not can_access_org_unit(conn, principal["member_id"], org_unit_id):
        return None
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM oams.org_units
           WHERE org_unit_id = %s AND status = 'active'""", (org_unit_id,)).fetchone()


def _department_row(conn, org_unit_id):
    """A department row for display only — no range check.

    Used when somebody opens a page another department shared with them: they have
    no business *in* that department (they cannot build pages there), but the page
    itself is open to them, and the header still has to say whose page it is.
    """
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM oams.org_units
           WHERE org_unit_id = %s AND status = 'active'""", (org_unit_id,)).fetchone()


def _page_access(conn, principal, page) -> tuple[bool, bool]:
    """(能不能看, 能不能填) for one page version and one person.

    Inside the department this is always (True, True) without touching the table:
    `_resolve_department` already answered, and the department's own people keep
    exactly the rights they have today. Outside it, the page's own audience table
    decides (`oams.interface_access`, written by the 页面设置 权限弹窗) — no row,
    no access. The new table therefore only ever **widens** what somebody sees;
    it can never take today's access away.
    """
    if _resolve_department(conn, principal, page["org_unit_id"]) is not None:
        return True, True
    member_id = principal["member_id"]
    return (security.can_view_interface(conn, member_id, page["org_unit_id"], page["slug"]),
            security.can_fill_interface(conn, member_id, page["org_unit_id"], page["slug"]))


def _access_note(conn, principal, page) -> str | None:
    """别人共享给我的页：谁开的、开到什么程度，一句话说清；本部门的人不看这句。

    句子在服务端拼，模板只按纯文本输出。判据用的是 `security.interface_access_via`
    ——它读的就是 `_page_access` 判可见性的那张表、同一条岗位链，这里不另写 SQL，
    也不另判一次「能不能看」（那件事只由 `_page_access` 回答）。

    口径来自负责人 2026-11-04：说部门不说人（权限是发给部门的）；只给查看就说
    「查看」，也给了编辑就说「查看和编辑」；公司根那行说「全公司可见」；点名部门与
    公司根同时命中时只提更具体的那个部门。
    """
    row = security.interface_access_via(conn, principal["member_id"],
                                        page["org_unit_id"], page["slug"])
    if row is None:
        return None
    owner = _department_row(conn, page["org_unit_id"])
    owner_name = (owner["name"] if owner else "别的部门")
    if row["unit_type"] == "company":
        return f"这是{owner_name}的页面：全公司可见。你能看到它，但不是这一页的主人。"
    name = row["name"] or row["org_code"]
    grant = f"{name}可查看、可编辑" if row["can_write"] else f"{name}可查看"
    return f"这是{owner_name}的页面：{grant}。你能到这里来，是因为这一页开给了{name}。"


def _access_editor(powers, org_unit_id) -> tuple[bool, str | None]:
    """能不能改这一页的权限，以及不能改的那句人话。

    改权限是**运营动作**，不是签字动作：系统管理员随时能改任何部门；部门管理员
    只改自己范围里的页。这跟 `_review_refusal`（审核）刻意分开——一页可能已经上线
    （不需要再审），但它的可见范围天天要调。
    """
    if powers["system_admin"]:
        return True, None
    if powers["department"] and org_unit_id in set(powers["units"]):
        return True, None
    return False, "改页面权限要系统管理员，或这一页所属部门的部门管理员。"


def _audience_text(audience) -> str:
    """这一页能被谁打开（「谁能查看」那一列）。列头已经说明含义，值里不再叠「只读」这种词。

    只数 `can_read` 的行：加了「可作参考」之后，一行可以只有参考权、看不到内容，
    那种行放进「谁能查看」就是在说假话。
    """
    readers = [row for row in audience if row["can_read"]]
    if not readers:
        return "只有本部门"
    parts = ["全公司" if row["unit_type"] == "company" else row["name"] for row in readers]
    return "本部门 + " + "、".join(parts)


def _audience_payload(audience) -> list[dict[str, Any]]:
    """审计里记的形态：按部门码写清能看/能改/能参考，别记 id（人看审计认不出 id）。"""
    return [{"org_code": row["org_code"], "can_read": row["can_read"],
             "can_write": row["can_write"],
             "can_base": bool(row.get("can_base"))} for row in audience]


def _fill_text(audience) -> str:
    """这一页能被谁编辑（「谁能编辑」那一列）：只列真有编辑权的部门。"""
    writers = [row for row in audience if row["can_write"]]
    if not writers:
        return "只有本部门"
    names = ["全公司" if row["unit_type"] == "company" else row["name"] for row in writers]
    return "本部门 + " + "、".join(names)


def _access_summary(row) -> str:
    """弹窗「说明」列：这一行现在是什么状态，用一句话说清。

    三格独立，所以不能说成「可查看、可编辑、可参考」——只勾了参考的人看不到数据，
    那句话会让人以为他能打开。按实际勾选拼，顺序固定：查看 > 编辑 > 参考。
    """
    if row.get("is_owner"):
        return "这一页属于它，天然可查看、可编辑、可参考"
    if row.get("closed_reason"):
        kept = row.get("can_read") or row.get("can_write") or row.get("can_base")
        return row["closed_reason"] + ("；已有授权会原样保留" if kept else "")
    granted = []
    if row.get("can_read"):
        granted.append("可查看")
    if row.get("can_write"):
        granted.append("可编辑")
    if row.get("can_base"):
        granted.append("可当模板")
    if not granted:
        return "—"
    if granted == ["可当模板"]:
        return "只能拿它当模板，看不到里面的数据"
    return "、".join(granted)


def _base_text(audience) -> str:
    """这一页能被谁当模板参考（「谁能参考」那一列）：只列真拿到参考权的部门。"""
    bases = [row for row in audience if row.get("can_base")]
    if not bases:
        return "只有本部门"
    names = ["全公司" if row["unit_type"] == "company" else row["name"] for row in bases]
    return "本部门 + " + "、".join(names)


def _entry_mode_for(status):
    """A page under review collects trial data; only an approved page feeds the company."""
    return "live" if status == "active" else "draft"


def _entry_mode_label(mode: str) -> str:
    """审计里给人看的说法：草稿版还在攒试用数据，现行版才是公司看到的那一份。"""
    return "现行版" if mode == "live" else "草稿版"


# --------------------------------------------------------------------------- #
# 查件框（lookup_box）：只查正在看的这一版自己的条目
#
# 形态见 docs/design/lookup-box.md，路由规则见契约 §1 与那一节的清单第 7 条。
# 三条边界写在这里，免得后面有人顺手放宽：
#   * 只查 `interface_id = 这一版`，不按 slug 跨版本、不 join 别页、不读汇总表；
#   * 查哪个字段由这一版的 descriptor 决定（`lookup_box.params.key_field`），
#     请求里带什么都忽略——否则换个查询串就能翻别人的字段；
#   * 空输入一条 SQL 都不发：没有单号就没有查询，回那句空态就够。
# --------------------------------------------------------------------------- #

# 有自己列的字段：值直接落在 interface_entries 的列上，其余（金额、单号、日期…）
# 落在 extra 里，键就是 descriptor 字段的 key。查件框要在两种存储上都查得到。
_ENTRY_COLUMNS = ("title", "status", "blocked_reason", "progress_value", "note", "evidence")

LOOKUP_EMPTY_PROMPT = '<p class="wh-empty">输入单号后在这里看结果。</p>'
LOOKUP_ARCHIVED_PROMPT = ('<p class="wh-empty">这一版已归档，查不到它的条目了；'
                          '刷新页面会回到这个页面现在的版本。</p>')


def _find_lookup_box(modules):
    """The first lookup_box in this descriptor's module tree, or None."""
    for module in modules or []:
        if not isinstance(module, dict):
            continue
        if module.get("type") == "lookup_box":
            return module
        found = _find_lookup_box(module.get("children"))
        if found is not None:
            return found
    return None


def _entry_field_value(entry, field):
    """Standard field value of a saved entry, from its column or from extra."""
    map_to = field.get("map_to")
    if map_to in _ENTRY_COLUMNS:
        return entry[map_to]
    return (entry.get("extra") or {}).get(field.get("key"))


def _lookup_fields(descriptor, key_field):
    """Descriptor fields that carry the standard field the box looks up."""
    return [field for field in descriptor.get("fields") or []
            if isinstance(field, dict) and field.get("key") and field.get("map_to") == key_field]


def _lookup_row(conn, interface_id, mode, descriptor, key_field, query):
    """The newest entry of this page whose key field equals the query.

    整页口径（`api.PAGE_ENTRY_SCOPE_SQL`，与列表、数字、状态统计同一个范围）：记在更早
    那一版的条目仍然是这一页的条目。否则会出现自相矛盾的画面——表格里列着这一行，
    上方的查件框却说「本页没有这个单号」，读的人只会判断查询坏了，而不是版本不同。

    仍然只查本页本部门（页面身份是 (org_unit_id, slug)，由那个常量解析），不跨页不跨
    部门；字段映射仍按**当前这一版**的 descriptor 取，老条目新加的字段没值就空着。
    """
    fields = _lookup_fields(descriptor, key_field)
    if not fields:
        return None, 0
    params: list[Any] = [interface_id, mode]
    if key_field in _ENTRY_COLUMNS:
        condition = f"e.{key_field} = %s"
        params.append(query)
    else:
        condition = "(" + " OR ".join("e.extra ->> %s = %s" for _ in fields) + ")"
        for field in fields:
            params.extend([field["key"], query])
    row = conn.execute(
        f"""SELECT e.*, count(*) OVER () AS same_number
              FROM oams.interface_entries e
             WHERE {api.PAGE_ENTRY_SCOPE_SQL}
               AND e.status_entry = 'active' AND e.entry_mode = %s
               AND {condition}
             ORDER BY e.reported_at DESC, e.entry_id DESC
             LIMIT 1""", params).fetchone()
    if row is None:
        return None, 0
    return row, int(row.get("same_number") or 1) - 1


def _entry_status_word(descriptor, row):
    """这一页给某条记录显示的状态名：部门自己的词优先，取不到才退回标准态的显示名。

    与表格那一列同一个口径、同一条优先级（`_interface_values` 里同一段说明）：条目里
    留着部门词（`extra[<字段 key>]`）就用它，否则用 `STATUS_LABELS`。两处各写一套的话，
    同一条记录会在同一屏上被叫两个名字——「待取」与「未开始」，读的人以为状态被改了。
    """
    for field in descriptor.get("fields") or []:
        if not isinstance(field, dict) or field.get("map_to") != "status" or not field.get("key"):
            continue
        word = (row.get("extra") or {}).get(field["key"])
        if word:
            return word
    raw = row.get("status") or ""
    return STATUS_LABELS.get(raw, raw)


def _lookup_hit(descriptor, row, key_field, query, current_interface_id=None):
    """Shape one entry for lookup_result.html: the page's own labels and words.

    卡片上每一行的标签都来自本页 descriptor 的 `label`，值走 `_entry_field_value`
    （标准列取列、其余取 `extra[字段key]`）。以前这里写死的是快递代收那套词
    ——「预计到件」「取件点」，`place` 还借 note 当取件点；换一个部门用查件框，
    卡片就在用别人的话说自己的记录。空值不进卡片：老条目没有的字段就是没有。
    """
    key_field_value = ""
    for field in _lookup_fields(descriptor, key_field):
        key_field_value = _entry_field_value(row, field) or ""
        if key_field_value:
            break
    rows: list[dict[str, str]] = []
    for field in descriptor.get("fields") or []:
        if not isinstance(field, dict) or not field.get("key"):
            continue
        if field.get("map_to") == "status":
            value = _entry_status_word(descriptor, row)
        else:
            value = _entry_field_value(row, field)
        value = "" if value is None else str(value).strip()
        if not value:
            continue
        row_value = {"label": str(field.get("label") or field["key"]), "value": value}
        href = _safe_link(value) if field.get("map_to") == "evidence_url" else ""
        if href:
            row_value["href"] = href
        rows.append(row_value)
    # 命中可能落在更早的那一版上（整页口径）：字段值按当前这一版映射，老条目里没有的
    # 字段就是空的。这件事要在卡片上说出来——不说，读到空字段的人会以为数据丢了。
    older = False
    if current_interface_id is not None and row.get("interface_id") is not None:
        older = str(row["interface_id"]) != str(current_interface_id)
    return {"number": key_field_value or query,
            "status": _entry_status_word(descriptor, row),
            "rows": rows,
            "older": older}


def _safe_link(value: str) -> str:
    """只放行 http/https 链接：底稿里的地址是部门自己写的，`javascript:` 一律当文本。"""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    return value


def _lookup_result_html(query, hit, more):
    """The one fragment both the partial response and the full page show."""
    return TEMPLATES.get_template("lookup_result.html").render(
        query=query, hit=hit, more=more)


def _lookup_fragment(conn, interface_id, mode, descriptor, query):
    """The result block for one query: hit card, miss line, or the empty prompt.

    Returns an empty string when this page has no lookup box at all -- the caller
    turns that into a 404, because a query against a page that cannot query is a
    wrong address, not an empty answer.
    """
    box = _find_lookup_box(descriptor.get("modules"))
    if box is None:
        return ""
    params = box.get("params") or {}
    key_field = params.get("key_field") if isinstance(params, dict) else None
    key_field = key_field.strip() if isinstance(key_field, str) and key_field.strip() else "order_no"
    if not _lookup_fields(descriptor, key_field):
        return ""
    text = (query or "").strip()
    if not text:
        return LOOKUP_EMPTY_PROMPT
    row, more = _lookup_row(conn, interface_id, mode, descriptor, key_field, text)
    hit = (_lookup_hit(descriptor, row, key_field, text, current_interface_id=interface_id)
           if row is not None else None)
    return _lookup_result_html(text, hit, more)


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
    api.require_app_scope(principal, "interfaces:draft")
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
                """SELECT interface_id, org_unit_id, slug, status, visibility
                     FROM oams.interfaces WHERE interface_id = %s""", (source_id,)).fetchone()
            openable = source is not None and source["status"] == "active" and (
                source["visibility"] == "public"
                or source["org_unit_id"] in active_org_unit_ids(conn, principal["member_id"])
                # 页面主人把「参考」开给了这个人的部门（`can_base`）：能拿它当模板，
                # 但这份权利不让他打开那一页，所以这里单独问一次，不走 can_view。
                or security.can_base_interface(conn, principal["member_id"],
                                               source["org_unit_id"], source["slug"]))
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
                  f"在部门工作台新建了页面草稿「{name.strip()}」",
                  {"slug": slug.strip(), "version_no": version,
                   "based_on": str(basis) if basis else None})
    return RedirectResponse(url=f"/departments/{org_unit_id}/{row['interface_id']}?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# 页面身份：`(org_unit_id, slug)` 是页面，`interface_id` 只是它某一版
#
# 契约 6.4。读旧版本要落到这一页现在的版本上——旧版本记的条目在新版本的数字里
# 照旧算数；但往旧版本里写必须拒，否则一个开着没关的旧标签页会往一个没人再看的
# 版本上追加条目，人却以为写进了页面。
# ---------------------------------------------------------------------------

ARCHIVED_WRITE_REFUSAL = ("这一版已归档，不能再往里写。刷新页面会回到这个页面现在的版本；"
                          "旧版本上记的条目在新的版本里照旧算数。")


def _current_version_id(conn, org_unit_id, slug):
    """The version a page currently publishes, or None when it has none."""
    row = conn.execute(
        "SELECT interface_id FROM oams.interfaces WHERE org_unit_id = %s AND slug = %s "
        "AND status = 'active'", (org_unit_id, slug)).fetchone()
    return row["interface_id"] if row else None


def _page_version(conn, org_unit_id, interface_id):
    """Any version of this page, archived included (the default lookups skip it)."""
    return conn.execute(
        "SELECT * FROM oams.interfaces WHERE interface_id = %s AND org_unit_id = %s",
        (interface_id, org_unit_id)).fetchone()


def _is_page_author(page, principal) -> bool:
    """这一页是不是本人建的。

    送审是「我写完了」的声明，替别人说这句话，审核人就分不清该找谁问；
    真要说的是别人，让作者自己点，或者走审核人的「退回」。
    老数据没有 created_by（种子/迁移留下的）时不当成别人的页。
    """
    author = page["created_by"]
    if author is None:
        return True
    return str(author) == str(principal.get("member_id"))


def _archived_reader_response(request: Request, conn, org_unit_id, row):
    """Send a reader of an archived version to the page's current one.

    The redirect carries ``?archived=1`` so the workbench can say why the page is
    not the one the link pointed at. Without it an old bookmark would silently show
    a different version than the reader asked for -- the numbers would be right and
    the surprise still theirs.
    """
    current = _current_version_id(conn, org_unit_id, row["slug"])
    if current is None:
        return _denied(request, "这一版已归档，这个页面目前没有在用的版本。")
    response = RedirectResponse(url=f"/departments/{org_unit_id}/{current}?archived=1",
                               status_code=303)
    return response


def _archived_writer_response(org_unit_id, row, conn):
    """Refuse a write aimed at an archived version, and say where to go instead."""
    current = _current_version_id(conn, org_unit_id, row["slug"])
    if current is None:
        return PlainTextResponse(ARCHIVED_WRITE_REFUSAL, status_code=409)
    body = (ARCHIVED_WRITE_REFUSAL + "\n\n现在的版本：/departments/" + str(org_unit_id)
            + "/" + str(current) + "\n")
    return PlainTextResponse(body, status_code=409)


@router.get("/pages/{org_code}/{slug}", response_class=HTMLResponse)
def page_by_slug(request: Request, org_code: str, slug: str):
    """The page's address that does not change when it is re-published.

    A page is identified by `(org_code, slug)`; the version id changes at every
    publish, so a link built from it rots the moment somebody edits the page. This
    route resolves which version is worth opening now and hands off to the
    workbench, which is the one place the page is actually drawn.

    "Worth opening" is not only the published one: a department has to be able to
    open its own page while it is still a draft or waiting for review, otherwise
    the stable address would refuse exactly the people who are working on it (the
    two partial unique indexes allow at most one published and one open version,
    so the order is well defined). Callers outside the department never get this
    far -- the workbench re-checks the scope.
    """
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url=f"/login?next=/pages/{org_code}/{slug}", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门页面需要 workforce.reports/read 与 submit 权限。")
    with connection() as conn:
        row = conn.execute(
            """SELECT i.interface_id, i.org_unit_id
                 FROM oams.interfaces i
                 JOIN oams.org_units o ON o.org_unit_id = i.org_unit_id
                WHERE o.org_code = %s AND i.slug = %s AND i.status <> 'archived'
                ORDER BY (i.status = 'active') DESC, i.version_no DESC
                LIMIT 1""",
            (org_code, slug)).fetchone()
        if row is None:
            return _denied(request, "这个页面不存在，或者还没有可打开的版本。")
        can_view, _ = _page_access(conn, principal,
                                   {"org_unit_id": row["org_unit_id"], "slug": slug})
        if not can_view:
            return _denied(request, "你不在这个部门的范围内。")
    return RedirectResponse(url=f"/departments/{row['org_unit_id']}/{row['interface_id']}",
                            status_code=303)


@router.get("/departments/{org_unit_id}/{interface_id}", response_class=HTMLResponse)
def department_page(request: Request, org_unit_id: UUID, interface_id: UUID,
                    saved: str | None = None, error: str | None = None,
                    archived: str | None = None, lookup: str | None = None):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门页面需要 workforce.reports/read 与 submit 权限。")
    return _workbench_page(request, principal, org_unit_id, interface_id,
                           saved=saved, error=error, archived=archived,
                           lookup_query=lookup)


@router.get("/departments/{org_unit_id}/{interface_id}/lookup", response_class=HTMLResponse)
def department_lookup(request: Request, org_unit_id: UUID, interface_id: UUID,
                      q: str | None = None, lookup: str | None = None):
    """查件框的局部请求入口：只查正在看的这一版，只回结果那一块。

    两条地址差异是故意的：带 ``HX-Request`` 的 htmx 请求回片段（结果区自己换），
    直接点开这个地址的人回整页（带上 ``?lookup=``），因为裸片段没有外壳、没有
    页签、也没有「这页是哪一页」。``q`` 是契约里写的参数名，``lookup`` 是模板
    输入框的 name，两个都收，免得两边对一个字母。
    """
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门页面需要 workforce.reports/read 与 submit 权限。")
    text = (q if q is not None else lookup) or ""
    if not request.headers.get("HX-Request"):
        target = f"/departments/{org_unit_id}/{interface_id}"
        if text.strip():
            target += "?lookup=" + urllib.parse.quote(text.strip())
        return RedirectResponse(url=target, status_code=303)
    with connection() as conn:
        page = _page_version(conn, org_unit_id, interface_id)
        if page is None:
            return HTMLResponse('<p class="wh-empty">这个页面不存在或不属于该部门。</p>',
                                status_code=404, headers={"Cache-Control": "no-store"})
        can_view, _ = _page_access(conn, principal, page)
        if not can_view:
            return HTMLResponse('<p class="wh-empty">你不在这个部门的范围内。</p>',
                                status_code=403, headers={"Cache-Control": "no-store"})
        if page is None:
            return HTMLResponse('<p class="wh-empty">这个页面不存在或不属于该部门。</p>',
                                status_code=404, headers={"Cache-Control": "no-store"})
        if page["status"] == "archived":
            # 片段里不能塞一个 303：XHR 会自己跟过去，把整页 HTML 换进结果区。
            return HTMLResponse(LOOKUP_ARCHIVED_PROMPT, status_code=200,
                                headers={"Cache-Control": "no-store"})
        body = _lookup_fragment(conn, interface_id, _entry_mode_for(page["status"]),
                                page["descriptor"], text)
    if not body:
        return HTMLResponse('<p class="wh-empty">这一页没有查件框。</p>', status_code=404,
                            headers={"Cache-Control": "no-store"})
    return HTMLResponse(body, status_code=200, headers={"Cache-Control": "no-store"})


def _workbench_page(request: Request, principal: dict[str, Any], org_unit_id: UUID,
                    interface_id: UUID, *, saved: str | None = None, error: str | None = None,
                    archived: str | None = None,
                    values: dict[str, Any] | None = None, status_code: int = 200,
                    lookup_query: str | None = None):
    """Draw the department workbench for one page.

    ``values`` overrides what the fields show. A submission the server refused comes
    back through here with what the person typed -- re-rendering the last *saved*
    entry instead would quietly throw their words away.

    ``archived`` is the query flag set by :func:`_archived_reader_response` when the
    version in the link was archived; the template turns it into one line saying the
    reader was moved to the current version. It only carries a notice -- the page
    itself is the current version either way.
    """
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        page = _page_version(conn, org_unit_id, interface_id)
        if page is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        # 部门内恒为能看能填；部门外只看页面自己的可见范围（页面设置里授权的那些部门）。
        can_view, can_fill = _page_access(conn, principal, page)
        if not can_view:
            return _denied(request, "你不在这个部门的范围内。")
        if unit is None:
            # 别人共享给我的页：我没有这个部门的业务，只为页头显示「这是谁的页」。
            unit = _department_row(conn, org_unit_id)
            if unit is None:
                return _denied(request, "这个页面不存在或不属于该部门。")
            # 页头那句「谁开的」：只有靠授权跨部门进来的人才有，本部门的人（主人）没有。
            access_note = _access_note(conn, principal, page)
        else:
            access_note = None
        if page["status"] == "archived":
            # Contract 6.4: the page is addressed by (org_unit_id, slug); this row
            # is only one version of it, and the entries recorded here still count
            # on the current one. Send the reader there instead of a dead end.
            return _archived_reader_response(request, conn, org_unit_id, page)
        mode = _entry_mode_for(page["status"])
        descriptor = page["descriptor"]
        if lookup_query is not None and request.headers.get("HX-Request"):
            # 查件框的 htmx 请求打在页面地址上（模板里 hx-get="?"），所以整页路由
            # 也要能回那一块：一条查询只有一份实现，别在两条路由里各写一遍。
            body = _lookup_fragment(conn, interface_id, mode, descriptor, lookup_query)
            if not body:
                return HTMLResponse('<p class="wh-empty">这一页没有查件框。</p>', status_code=404,
                                    headers={"Cache-Control": "no-store"})
            return HTMLResponse(body, status_code=200, headers={"Cache-Control": "no-store"})
        # 直接点开 /lookup?q=… 的人回到整页，且页面上那一块已经是他要的结果：
        # 两条路径同一份片段、同一句话，别让「整页」和「片段」各说一套。
        lookup_html = (_lookup_fragment(conn, interface_id, mode, descriptor, lookup_query)
                       if lookup_query is not None else "")
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
        # `rollup` above already holds the numbers this page declared (`dept.*`);
        # without handing them to the renderer every display module shows "—",
        # because a metric_card cannot compute anything by itself.
        rendered = render_interface(descriptor, values=values, include_unbound=True,
                                    aggregates=rollup.get("aggregates") or {},
                                    lookup=lookup_html)
    except RenderError as exc:
        return _render_failed(request, page=page, exc=exc, org_name=unit["name"],
                              back_href=f"/departments/{org_unit_id}", back_label="返回部门")
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    return TEMPLATES.TemplateResponse(request, "department_workbench.html", {
        "unit": unit, "departments": departments, "pages": pages,
        # 只读共享：能看这一页、但填写由所属部门负责（模板据此收起表单，只留一行说明）。
        "can_fill": can_fill,
        # 靠授权跨部门进来时的一句话「谁开的」；本部门与系统管理员看自己的页时是 None。
        "access_note": access_note,
        "current": page, "page": page, "rendered": rendered, "recent": recent,
        "preview": page["status"] != "active", "draft_mode": page["status"] != "active",
        "page_status_label": {"draft": "待提交审核", "pending": "审核中"}.get(page["status"], page["status"]),
        "live_version": live_version,
        "review_code_label": (review_rules.rejection_label(page.get("review_code"))
                              if page.get("review_code") else None),
        "member": _member(request), "saved": saved, "error": error,
        # Set only when a link pointed at an archived version and we sent the reader
        # to the current one; the template shows one line explaining the swap.
        "archived_notice": bool(archived),
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
    api.require_app_scope(principal, "entries:write")
    _check_form_token(request, csrf, "oams:interface-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        page = _page_version(conn, org_unit_id, interface_id)
        if page is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        # 能看≠能填：别的部门可以只读共享这一页，那时候表单一颗字都不该收。
        can_view, can_fill = _page_access(conn, principal, page)
        if not can_view:
            return _denied(request, "你不在这个部门的范围内。")
        if not can_fill:
            owner = _department_row(conn, page["org_unit_id"])
            return _denied(request, f"这一页你只能查看，不能编辑：这一页的内容由「{owner['name'] if owner else '所属部门'}」负责。")
        if page["status"] == "archived":
            return _archived_writer_response(org_unit_id, page, conn)
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
                  entry["entry_id"],
                  f"在部门页面填了一条「{entry['title']}」（{_entry_mode_label(mode)}）", entry)
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
    api.require_app_scope(principal, "entries:write")
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
            page = _page_version(conn, org_unit_id, interface_id)
            if page is None:
                return _denied(request, "这个页面不存在或不属于该部门。")
            can_view, can_fill = _page_access(conn, principal, page)
            if not can_view:
                return _denied(request, "你不在这个部门的范围内。")
            if not can_fill:
                return _denied(request, "这一页你只能查看，不能编辑它的条目：这一页的内容由所属部门负责。")
            if page["status"] == "archived":
                # The entry itself is still the page's entry (contract 6.4), but a
                # write aimed at an archived version is refused like the other three
                # entry paths: a form left open in an old tab must not edit a version
                # nobody is looking at. Linking from the current version works.
                return _archived_writer_response(org_unit_id, page, conn)
            entry = conn.execute(
                """SELECT e.entry_id, e.interface_id, e.title FROM oams.interface_entries e
                   WHERE e.entry_id = %s AND e.interface_id IN (
                       SELECT v.interface_id FROM oams.interfaces v
                        WHERE v.org_unit_id = %s AND v.slug = %s)""",
                (entry_id, org_unit_id, page["slug"])).fetchone()
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
                      (f"把条目「{entry['title']}」挂到了工作项 {target}" if target is not None
                       else f"把条目「{entry['title']}」从工作项上摘了下来"), entry)
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
        page = _page_version(conn, org_unit_id, interface_id)
        can_view = page is not None and _page_access(conn, principal, page)[0]
        if not can_view:
            return JSONResponse(status_code=403, content={"available": False, "reason": "not in this department"})
        api.require_app_scope(principal, "entries:read:dept")
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
    api.require_app_scope(principal, "interfaces:draft")
    _check_form_token(request, review_csrf, "oams:page-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        prior = _page_version(conn, org_unit_id, interface_id)
        if prior is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        if prior["status"] == "archived":
            return _archived_writer_response(org_unit_id, prior, conn)
        if prior["status"] == "pending":
            return RedirectResponse(url=base + "?error=" + urllib.parse.quote("这个页面已经提交，正在等审核"),
                                    status_code=303)
        if prior["status"] == "active":
            return RedirectResponse(url=base + "?error=" + urllib.parse.quote("这个页面已经启用，无需再次提交"),
                                    status_code=303)
        if not _is_page_author(prior, principal):
            return RedirectResponse(
                url=base + "?error=" + urllib.parse.quote("这一页不是你建的，别替别人送审——让作者自己送"),
                status_code=303)
        conn.execute(
            """UPDATE oams.interfaces
                  SET status = 'pending', review_stage = 'department', review_note = NULL, updated_at = now()
               WHERE interface_id = %s""", (interface_id,))
        app_name = _app_name_for(conn, principal)
        api.audit(conn, principal["member_id"], "interface.review_requested", "interface", interface_id,
                  (f"通过 {app_name} 代表 {principal.get('display_name') or '这个人'} 提交了页面送审，等公司审"
                   if app_name else "提交了页面送审，等公司审"),
                  {"from": prior["status"], "to": "pending"})
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
            """SELECT * FROM oams.interfaces
               WHERE interface_id = %s AND org_unit_id = %s FOR UPDATE""",
            (interface_id, org_unit_id)).fetchone()
        if prior is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        if prior["status"] == "archived":
            return _archived_writer_response(org_unit_id, prior, conn)
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
                  "撤回了送审，页面回到草稿", {"from": "pending", "to": "draft"})
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



#: What a member may sign off, and where -- resolved once, read everywhere.
def _review_powers(conn, principal: dict[str, Any]) -> dict[str, Any]:
    """Who this click may sign for -- resolved once, read everywhere.

    三种人，三条规矩（2026-11-04 老大定的模型）：

    * **系统管理员**（`workforce.grants/manage`）不受审核限制：任何部门、任何
      阶段，包括自己建的、自己提交的页。他点不动自己那一页，是把「作者回避」
      当成了审核规矩，而那是旧模型。
    * **部门管理员**（`workforce.interfaces/review`）只签自己所在部门的页，但
      **可以签自己提交的**：现实里主管就是那个签字人。
    * **普通人**没有审核权。

    审核是**人**的动作：一张接入方的票（`connected-app:`）只有在人被允许时勾了
    `interfaces:review`、并且这个人自己本来就有审核权时才放行——应用永远不能比
    它代表的那个人签得更多。
    """
    member_id = principal["member_id"]
    app_id = api.app_id_of(principal)
    app_review = False
    if app_id is not None:
        authorization = api.live_app_authorization(principal)
        app_review = authorization is not None and "interfaces:review" in (
            authorization["scopes"] or ())
    return {
        "member_id": member_id,
        "system_admin": has_company_scope(conn, member_id),
        "department": has_permission(conn, member_id, "workforce.interfaces", "review"),
        "company": has_permission(conn, member_id, "workforce.interfaces", "review_company"),
        "units": accessible_org_unit_ids(conn, member_id),
        # 「管得到」与「坐在这」是两件事：前者含被授权代管的部门，后者只看岗位任职。
        # 归属（department / company_proxy）只认后者——公司级账号替一个没有 eigene
        # 主管的部门按下去了，记录里必须写「代签」，不能写成那个部门自己签的。
        "seat_units": active_org_unit_ids(conn, member_id),
        "app_id": app_id,
        "app_review": app_review,
    }


def _review_refusal(powers: dict[str, Any], page: Any) -> str | None:
    """Why this member may not decide this page, or None when they may.

    The queue asks it to draw buttons and both POST routes ask it before
    writing: one rule, so a page can never show "approve" and then refuse the
    click.

    2026-11-04 换了模型：拒签的理由不再是「作者回避」——部门主管签自己部门的
    页、系统管理员签任何页。现在唯一被挡的是**接入方替人签**：它得既能签这个
    范围（允许时勾了「替你签页面审核」），它代表的那个人又真的够权限。
    """
    if page["status"] != "pending":
        return "这个页面不在待审核状态"
    if powers["app_id"] is not None:
        if not powers["app_review"]:
            return ("这是接入方的动作，审核要人自己点：代表你的应用没有被允许"
                    "「替你签页面审核」")
        if not (powers["system_admin"] or powers["department"] or powers["company"]):
            return "这是接入方的动作，审核要人自己点：你本人没有审核权限"
    if powers["system_admin"]:
        return None
    if page["review_stage"] == "company":
        return None if powers["company"] else "公司终审需要 workforce.interfaces/review_company 权限"
    if powers["department"] and page["org_unit_id"] in powers["units"]:
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

    `department` and `company` are the two normal ones. `company_proxy` is the
    system administrator taking a department's first level without sitting in
    that department: the record has to say so, because that department never
    signed the version and a later reader must not be told otherwise.
    """
    if page["review_stage"] == "company":
        return "company"
    if powers["department"] and page["org_unit_id"] in powers["seat_units"]:
        return "department"
    return "company_proxy"



def _basis_pages(conn, member_id, org_unit_id: UUID) -> list[Any]:
    """Other departments' live pages this person may start a new page from.

    Three ways a page qualifies, and only these three: it is a public template,
    it belongs to a department this person can already enter, or its owner gave
    this person's department the **参考** right (`can_base`). A picker that lists
    pages it would then refuse invites people to click a dead option, and a list
    that names hidden pages leaks exactly the fact a 404 is there to withhold.

    The 参考 right is checked per page through `security.can_base_interface` rather
    than in SQL: that function owns the ancestor-chain rule, and repeating it here
    is how the two answers drift apart.
    """
    mine = list(active_org_unit_ids(conn, member_id))
    rows = conn.execute(
        """SELECT i.interface_id, i.slug, i.name, i.visibility, i.org_unit_id,
                  o.name AS org_name
             FROM oams.interfaces i JOIN oams.org_units o USING (org_unit_id)
            WHERE i.status = 'active' AND i.org_unit_id <> %s
            ORDER BY o.name, i.name""", (org_unit_id,)).fetchall()
    return [row for row in rows
            if row["visibility"] == "public"
            or row["org_unit_id"] in mine
            or security.can_base_interface(conn, member_id, row["org_unit_id"], row["slug"])]


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
              (note.strip() or "审核通过") + (f"｜{summary}" if summary else ""),
              {"to": "active", "version_no": page["version_no"], "stage": signed_level,
               "acting": acting, "changes": bundle["summary"]["total"] if bundle else 0})
    return None


@router.get("/reviews", response_class=HTMLResponse)
def review_queue(request: Request, saved: str | None = None, error: str | None = None):
    """「页面管理 · 待审核」：左侧栏三条里的第一条，也是这一节的落地屏。

    三条是**三个页面**，不是一页里的三个锚点——左侧栏是二级菜单，菜单项得是真页面。
    以前三条都是 `/reviews` 加锚点，而「待审核」那条正好就是页面本身，于是另外两条
    读起来像被塞进了它里面（用户原话：「为什么最近决定和各部门页面会在待审核里」）。
    """
    return _review_view(request, "pending", saved=saved, error=error)


@router.get("/reviews/settings", response_class=HTMLResponse)
def review_page_settings(request: Request, saved: str | None = None):
    """「页面管理 · 页面设置」：这个人管得到的每一页，连它现在谁能查看、谁能编辑。

    以前叫「各部门页面」——那说的是「有哪些页」，可这一屏真正要管的是**每页的设置**
    （谁能查看、谁能编辑，在这页上的权限弹窗里改），所以名字跟着用途走。
    """
    return _review_view(request, "settings", saved=saved)


@router.get("/reviews/decided", response_class=HTMLResponse)
def review_decided(request: Request):
    """「页面管理 · 最近决定」：谁在什么时候签了什么、按哪个理由退回。"""
    return _review_view(request, "decided")


#: 页面权限表单的令牌范围：与审核、填表的分开，三种令牌互不通用。
PAGE_ACCESS_PURPOSE = "oams:page-access-form:v1"


def _page_by_address(conn, org_code: str, slug: str):
    """一页的「现在该管的那一版」。

    地址是 (org_code, slug)，版本行只是外壳：权限挂在 (owner_org_unit_id, slug) 上，
    所以改标题、重发一版都不会把授权清零。挑选规则与 `/pages/{org_code}/{slug}` 同一条。
    """
    return conn.execute(
        """SELECT i.interface_id, i.org_unit_id, i.slug, i.name, i.status, i.version_no,
                  o.org_code, o.name AS org_name
             FROM oams.interfaces i
             JOIN oams.org_units o ON o.org_unit_id = i.org_unit_id
            WHERE o.org_code = %s AND i.slug = %s AND i.status <> 'archived'
            ORDER BY (i.status = 'active') DESC, i.version_no DESC
            LIMIT 1""",
        (org_code, slug)).fetchone()


def _company_root(conn):
    """公司根节点：它就是界面上的「全公司」。"""
    return conn.execute(
        """SELECT org_unit_id, name FROM oams.org_units
           WHERE unit_type = 'company' AND status = 'active'
           ORDER BY created_at LIMIT 1""").fetchone()


@router.get("/reviews/settings/{org_code}/{slug}/access", response_class=HTMLResponse)
def page_access_dialog(request: Request, org_code: str, slug: str,
                       error: str | None = None):
    """页面设置的权限弹窗：这一页现在谁能查看、谁能编辑，能改成什么。

    只读与可写由 `_access_editor` 判（系统管理员 / 该部门的部门管理员），灰态由
    `security.grantable_org_unit_ids` 给：**前端灰只是提示，保存时服务端再按人复核**。
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=" + request.url.path, status_code=302)
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = _page_by_address(conn, org_code, slug)
        if page is None:
            return _denied(request, "没有这一页，或者它还没有可管理的版本。")
        can_edit, why = _access_editor(powers, page["org_unit_id"])
        if not can_edit:
            return _denied(request, why)
        audience = security.interface_audience(conn, page["org_unit_id"], slug)
        granted = {row["org_unit_id"]: row for row in audience}
        grantable = security.grantable_org_unit_ids(conn, principal["member_id"])
        root = _company_root(conn)
        units = conn.execute(
            """SELECT org_unit_id, org_code, name, unit_type FROM oams.org_units
               WHERE status = 'active'
               ORDER BY (unit_type = 'company') DESC, org_code""").fetchall()
    choices = []
    for unit in units:
        is_owner = unit["org_unit_id"] == page["org_unit_id"]
        row = granted.get(unit["org_unit_id"])
        allowed = is_owner or grantable is None or unit["org_unit_id"] in grantable
        choices.append({
            "org_unit_id": unit["org_unit_id"], "org_code": unit["org_code"],
            "name": unit["name"], "unit_type": unit["unit_type"],
            "is_owner": is_owner, "is_company": root is not None and unit["org_unit_id"] == root["org_unit_id"],
            "can_read": is_owner or bool(row and row["can_read"]),
            "can_write": is_owner or bool(row and row["can_write"]),
            "can_base": is_owner or bool(row and row.get("can_base")),
            "allowed": allowed,
            "closed_reason": (None if allowed else
                              ("只有系统管理员能把页面开给全公司" if root is not None
                               and unit["org_unit_id"] == root["org_unit_id"]
                               else f"你没有「{unit['name']}」的管理范围")),
        })
        choices[-1]["summary"] = _access_summary(choices[-1])
    return TEMPLATES.TemplateResponse(request, "access_dialog.html", {
        "page": page, "choices": choices, "audience": audience,
        "audience_text": _audience_text(audience),
        "error": error, "csrf": _form_token(request, PAGE_ACCESS_PURPOSE),
        "company_root": root,
    })


@router.post("/reviews/settings/{org_code}/{slug}/access")
async def page_access_save(request: Request, org_code: str, slug: str, csrf: str = Form(...)):
    """保存这一页的可见范围：本部门永远是能看能填，其余按勾选。

    服务端按**这个人**复核一遍（`grantable_org_unit_ids`）——前端灰只是提示，
    不是权限；出现越界的部门就整笔拒绝，不落库、不留半截。
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=" + request.url.path, status_code=302)
    _check_form_token(request, csrf, PAGE_ACCESS_PURPOSE)
    form = await request.form()
    read_ids = {str(value) for value in form.getlist("read")}
    write_ids = {str(value) for value in form.getlist("write")}
    base_ids = {str(value) for value in form.getlist("base")}
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = _page_by_address(conn, org_code, slug)
        if page is None:
            return _denied(request, "没有这一页，或者它还没有可管理的版本。")
        can_edit, why = _access_editor(powers, page["org_unit_id"])
        if not can_edit:
            return _denied(request, why)
        grantable = security.grantable_org_unit_ids(conn, principal["member_id"])
        owner_id = page["org_unit_id"]
        wanted: list[dict[str, Any]] = []
        for raw in sorted(write_ids | read_ids | base_ids):
            try:
                unit_id = UUID(raw)
            except ValueError:
                continue
            if unit_id == owner_id:
                continue  # 本部门不写表：它永远能看能改能参考
            # 能编辑必然能查看（表上有 CHECK）；「能参考」不蕴含能查看——参考的是
            # 页面定义，不是条目内容，两件事分开勾。
            wanted.append({"org_unit_id": unit_id,
                           "can_read": raw in read_ids or raw in write_ids,
                           "can_write": raw in write_ids,
                           "can_base": raw in base_ids})
        if wanted:
            known = conn.execute(
                """SELECT org_unit_id, name FROM oams.org_units
                   WHERE status = 'active' AND org_unit_id = ANY(%s)""",
                ([row["org_unit_id"] for row in wanted],)).fetchall()
            names = {row["org_unit_id"]: row["name"] for row in known}
            missing = [row["org_unit_id"] for row in wanted if row["org_unit_id"] not in names]
            if missing:
                return RedirectResponse(
                    url=f"/reviews/settings/{org_code}/{slug}/access?error="
                        + urllib.parse.quote("这些部门不在用，不能授权"),
                    status_code=303)
            if grantable is not None:
                over = [names[row["org_unit_id"]] for row in wanted
                        if row["org_unit_id"] not in grantable]
                if over:
                    return RedirectResponse(
                        url=f"/reviews/settings/{org_code}/{slug}/access?error="
                            + urllib.parse.quote("你没有这些部门的管理范围：" + "、".join(over)),
                        status_code=303)
        before = security.interface_audience(conn, owner_id, slug)
        if grantable is not None:
            # 管不到的部门：它本来就有权限的话原样留着。看不见不等于可以撤掉——
            # 否则一个部门管理员保存一次，就把系统管理员给别人的授权悄悄删了。
            keep = [{"org_unit_id": row["org_unit_id"], "can_read": row["can_read"],
                     "can_write": row["can_write"], "can_base": bool(row.get("can_base"))}
                    for row in before if row["org_unit_id"] not in grantable]
            wanted.extend(keep)
        security.set_interface_audience(conn, owner_id, slug, wanted,
                                        granted_by=principal["member_id"])
        after = security.interface_audience(conn, owner_id, slug)
        api.audit(conn, principal["member_id"], "interface.access_changed", "interface",
                  page["interface_id"],
                  f"改了「{page['name']}」的可见范围：现在{_audience_text(after)}",
                  after={"audience": _audience_payload(after)},
                  before={"audience": _audience_payload(before)},
                  metadata={"owner_org_unit_id": str(owner_id), "slug": slug,
                            "org_code": page["org_code"],
                            "audience": _audience_payload(after)})
    return RedirectResponse(url="/reviews/settings?saved="
                            + urllib.parse.quote(f"「{page['name']}」的权限已保存"),
                            status_code=303)


def _review_view(request: Request, view: str, saved: str | None = None,
                 error: str | None = None):
    """Two queues, because two people sign: the department first, the platform last.

    A reviewer sees what they can decide -- a department lead the first-level
    pages of their own departments, the platform what the departments passed up --
    plus, for the platform, a read-only list of everything still pending, so
    "why is this page not live yet" has an answer without anyone guessing.

    `view` 只决定模板画哪一段；三段数据一次取齐（各自都是一页的量级），
    菜单之间切换不用维护三套查询。
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=" + request.url.path, status_code=302)
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
        # 页面设置这一屏：这个人能管到的每一页，连它现在谁能看、谁能填。
        # 谁能看/谁能填是**页面自己的属性**（`oams.interface_access`），跟 `visibility`
        # 那个「公共模板」不是一回事——后者只表示别的部门能拿它当底子另建一版。
        catalog = conn.execute(
            """SELECT i.interface_id, i.org_unit_id, i.slug, i.name, i.status, i.version_no, i.visibility,
                      o.name AS org_name, o.org_code,
                      (SELECT count(*) FROM oams.interface_entries e
                        WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               WHERE i.status IN ('active', 'pending')
                 AND (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""",
            (powers["system_admin"], list(powers["units"]))).fetchall()
        # 每页现在谁能看、谁能填（`oams.interface_access` 那几行），外加「这个人能不能改」。
        # 一次取齐：清单是一页的量级，别让模板为每行再要一次数据。
        catalog = [dict(
            row,
            audience=security.interface_audience(conn, row["org_unit_id"], row["slug"]),
        ) for row in catalog]
        for row in catalog:
            row["audience_text"] = _audience_text(row["audience"])
            row["fill_text"] = _fill_text(row["audience"])
            row["base_text"] = _base_text(row["audience"])
            row["can_edit_access"], row["access_why"] = _access_editor(powers, row["org_unit_id"])
    mine = [row for row in awaiting if _review_refusal(powers, row) is None]
    others = [row for row in awaiting if powers["company"] and _review_refusal(powers, row) is not None]
    # Every pending page is read as a diff against the version it was written
    # against, not as a whole page: that is the only part a human still has to
    # think about, and the part where a quiet re-mapping would otherwise slip by.
    with connection() as conn:
        review_cards = [dict(row, bundle=_review_bundle(conn, row),
                             acting=_acting_stage(powers, row),
                             # 签之前先试着画一遍：画不出来的页不该走到审核人手里
                             draw_health=_draw_health(row["descriptor"]))
                        for row in mine]
    # The decision list reads a code, not prose: "what do departments keep getting
    # wrong" has to be answerable by counting, not by re-reading old notes.
    decided_cards = [dict(row, code_label=review_rules.rejection_label(row["review_code"]))
                     for row in decided]
    return TEMPLATES.TemplateResponse(request, "reviews.html", {
        "view": view,
        "pending": review_cards, "others": others, "decided": decided_cards,
        "catalog": catalog, "member": principal,
        "powers": powers, "saved": saved, "error": error, "reasons": review_rules.REJECTION_CHOICES,
        "csrf": _form_token(request, "oams:review-form:v1")})


#: Statuses a reviewer may look at a drawn version in: the one being decided, and
#: the one already live (looking back at what was signed off).
PREVIEW_STATUSES = ("pending", "active")


def _preview_refusal(powers: dict[str, Any], page: Any) -> str | None:
    """Why this member may not open the preview of this version, or None.

    Same predicate as `approve` -- one rule, so a card can never offer a preview
    the server then refuses. Only the first branch is widened: `_review_refusal`
    requires `status = 'pending'`, while a version that already went live is a
    page the same people are still entitled to look at. Everything else (who may
    sign which department and stage, and whether an app is allowed to sign for
    somebody) is asked unchanged, by handing the same row back as pending.
    """
    if page["status"] not in PREVIEW_STATUSES:
        return "这个页面现在既不在待审核、也没有上线，没有可预览的版本"
    if page["status"] == "pending":
        return _review_refusal(powers, page)
    probe = dict(page)
    probe["status"] = "pending"
    return _review_refusal(powers, probe)


def _preview_reason(exc: Exception) -> str:
    """One sentence a reviewer can act on, without quoting the traceback.

    A descriptor that calls a module nobody registers any more (the platform
    indicators and their modules went away with the product hub) is a fact about
    *that version*, not a server failure. The exception text never reaches the
    page whole: only the module name it names is quoted, the rest goes to the log.
    """
    text = str(exc)
    marker = "未注册的模块类型 '"
    at = text.find(marker)
    module = text[at + len(marker):].split("'", 1)[0] if at != -1 else ""
    if module:
        return f"{module} 已下线"
    if isinstance(exc, RenderError):
        return "描述符里有渲染器不认识的写法"
    return f"渲染时抛了 {type(exc).__name__}，细节在服务端日志里"


def _preview_degraded(exc: Exception) -> str:
    """What the card shows when this version cannot be drawn at all."""
    return (
        '<div class="preview-degraded" data-degraded="1" role="status">'
        '<p class="caption">预览不可用</p>'
        f'<p class="sub">这一版渲不出来：{html_text.escape(_preview_reason(exc))}。'
        '先按差异表审，或让作者改完重发。</p>'
        '</div>')


def _draw_health(descriptor: Any) -> dict[str, Any]:
    """Can this version be drawn at all? Asked before the reviewer signs.

    Rendering is the only honest probe: this is the same call the preview route
    makes (`render_preview`, sample values, no department data). A page used to
    pass validation, pass review, and then answer 500 the first time somebody
    opened it -- the reviewer had signed a definition nobody had ever drawn.
    """
    try:
        render_preview(descriptor or {})
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot draw"
        _RENDER_LOG.warning("draw health: page would not render: %s", exc)
        return {"ok": False, "reason": _preview_reason(exc)}
    return {"ok": True, "reason": ""}


def _preview_problem(request: Request, message: str, *, status_code: int):
    """Refusals from the preview route keep their own status and their own shape.

    The route is an htmx fragment target, not a page anybody navigates to: a
    redirect here would swap the landing page into the review card, and a 200
    with an empty body would look like a version that draws nothing. So the
    status says what happened (403 not allowed, 404 no such page, 500 will not
    render) and the body says why -- as a one-block fragment when htmx asked
    (it would otherwise paste a whole page inside the card), as a normal page
    when a person opened the URL.
    """
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f'<div class="flash error" role="alert">{html_text.escape(message)}</div>',
            status_code=status_code)
    return TEMPLATES.TemplateResponse(request, "denied.html",
                                      {"message": message, "member": _member(request)},
                                      status_code=status_code)


@router.get("/reviews/{interface_id}/preview", response_class=HTMLResponse)
def review_preview(request: Request, interface_id: UUID):
    """Draw one version the way it will look live -- from its descriptor alone.

    The reviewer signs off "can this definition go live", so what is drawn is the
    definition: `render_preview(sample=True)` takes every value from
    `_sample_values()` / `_sample_aggregates()` (status word, `example`, a neutral
    128 for the page's own emit metrics) and **no department's real entries**. This
    route therefore never reads `interface_entries`, never calls
    `build_department_aggregates`, and passes neither `values` nor `aggregates`:
    that is exactly what separates it from `/interfaces/{id}`, which is the
    "fill in real data" screen and needs `reports/read + submit` plus a live page.
    """
    principal = _member(request)
    if principal is None:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = conn.execute(
            """SELECT i.interface_id, i.name, i.slug, i.status, i.version_no, i.schema_version,
                      i.descriptor, i.org_unit_id, i.review_stage, i.updated_by, i.based_on,
                      o.name AS org_name
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               WHERE i.interface_id = %s""", (interface_id,)).fetchone()
    if page is None:
        return _preview_problem(request, "找不到这个页面。", status_code=404)
    refusal = _preview_refusal(powers, page)
    if refusal is not None:
        return _preview_problem(request, refusal + "。", status_code=403)
    try:
        # descriptor only: no connection is open here on purpose.
        html = render_preview(page["descriptor"] or {})
    except RenderError as exc:
        # A version that cannot be drawn is the descriptor's problem, not the
        # route's: the reviewer gets a sentence she can act on, and the details
        # go to the log rather than into a 500 the card cannot render. A real
        # 500 stays for the failures that mean the route itself is broken.
        _RENDER_LOG.warning(
            "preview degraded: interface=%s slug=%s schema=%s org=%s error=%s",
            page.get("interface_id"), page.get("slug"), page.get("schema_version"),
            page.get("org_name"), exc)
        return HTMLResponse(_preview_degraded(exc), status_code=200)
    response = HTMLResponse(html)
    # A version changes while it waits in the queue; a cached preview would show
    # the reviewer the previous drawing.
    response.headers["Cache-Control"] = "no-store"
    return response


def _approve_page(conn, principal: dict[str, Any], page: Any, note: str,
                  acting: str) -> tuple[str | None, str | None]:
    """这一票盖下去之后发生的事：返回（跳回列表的 saved 值，失败原因）。

    部门那一级，只给自己用的页当场上线；向外/进公司汇总的页只是换了级别，等平台再签。
    表单与 `/v1` 那条 JSON 出口共用这一个函数——同一颗按钮、同一套后果。
    """
    if page["review_stage"] == "company":
        return "approved", _activate_page(conn, principal, page, note, acting=acting)
    escalate = review_rules.needs_company_review(
        page["descriptor"] or {}, cross_department=_cross_department(conn, page))
    if not escalate:
        return "approved", _activate_page(conn, principal, page, note, acting=acting)
    conn.execute(
        """UPDATE oams.interfaces SET review_stage = 'company', updated_at = now()
           WHERE interface_id = %s""", (page["interface_id"],))
    api.audit(conn, principal["member_id"], "interface.review_stage_passed", "interface",
              page["interface_id"],
              note.strip() or "部门一审通过，转公司审",
              {"from": "department", "to": "company", "version_no": page["version_no"],
               "stage": "department", "acting": acting})
    return "passed", None


def _reject_page(conn, principal: dict[str, Any], page: Any, note: str,
                 code: str | None, acting: str) -> None:
    """退回一版：页子回到草稿，并从第一级重新开始。"""
    conn.execute(
        """UPDATE oams.interfaces
              SET status = 'draft', review_stage = 'department',
                  reviewed_by = %s, reviewed_at = now(),
                  review_note = %s, review_code = %s, updated_at = now()
            WHERE interface_id = %s""",
        (principal["member_id"], note.strip(), code, page["interface_id"]))
    api.audit(conn, principal["member_id"], "interface.rejected", "interface",
              page["interface_id"], note.strip(),
              {"to": "draft", "review_code": code,
               "from_stage": page["review_stage"], "acting": acting})


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
        saved, failed = _approve_page(conn, principal, page, note, acting)
        if failed:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote(failed), status_code=303)
    return RedirectResponse(url=f"/reviews?saved={saved}", status_code=303)


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
        _reject_page(conn, principal, page, note, code, _acting_stage(powers, page))
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
                      i.org_unit_id, o.name AS org_name, o.org_code AS org_code,
                      p.title AS position_title,
                      (SELECT count(*) FROM oams.interface_entries e
                        WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.positions p ON p.position_id = i.position_id
               WHERE i.status = 'active' AND (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""", (company_scope, scoped)).fetchall()
        # 别的部门开给我看的页也要出现在这里，否则「勾了能查看」等于只给了一个
        # 得自己背下来的网址——授权在界面上没有落点。判据不另写：逐页问
        # `security.can_view_interface`（与打开那一页时用的是同一个函数）。
        # 候选只取「不属于我管的部门」的页，通常只有个位数，列表页不是热路径。
        mine = {row["interface_id"] for row in rows}
        candidates = conn.execute(
            """SELECT i.interface_id, i.slug, i.name, i.visibility, i.version_no,
                      i.org_unit_id, o.name AS org_name, o.org_code AS org_code,
                      p.title AS position_title,
                      (SELECT count(*) FROM oams.interface_entries e
                        WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
               FROM oams.interfaces i
               JOIN oams.org_units o USING (org_unit_id)
               LEFT JOIN oams.positions p ON p.position_id = i.position_id
               WHERE i.status = 'active' AND NOT (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""", (company_scope, scoped)).fetchall()
        shared = [row for row in candidates
                  if row["interface_id"] not in mine
                  and security.can_view_interface(conn, principal["member_id"],
                                                  row["org_unit_id"], row["slug"])]
        rows = list(rows) + shared
    return TEMPLATES.TemplateResponse(request, "interfaces.html", {
        "interfaces": rows, "mine": mine, "member": _member(request),
        "saved": saved, "error": error})



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
                   error: str | None = None, lookup: str | None = None):
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
        if lookup is not None and request.headers.get("HX-Request"):
            # 填报表单页也画查件框（同一个 render_interface(edit)），模板里
            # hx-get="?" 打的是页面地址，所以这条路由必须跟工作台一样回那一块。
            # 少了这个分支，整页 HTML 会被 hx-swap 灌进结果区：盒子当场从一百多
            # 像素撑成一千多（实测 119 → 1007），换页之后才恢复。
            body = _lookup_fragment(conn, interface_id, mode, row["descriptor"], lookup)
            if not body:
                return HTMLResponse('<p class="wh-empty">这一页没有查件框。</p>', status_code=404,
                                    headers={"Cache-Control": "no-store"})
            return HTMLResponse(body, status_code=200, headers={"Cache-Control": "no-store"})
        latest = conn.execute(
            """SELECT * FROM oams.interface_entries
               WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
               ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
        # 整页带 ?lookup= 打开时，结果区里放的必须是已经算好的那一块——否则同一
        # 个单号在片段和整页上会显示两样东西。
        lookup_html = (_lookup_fragment(conn, interface_id, mode, row["descriptor"], lookup)
                       if lookup is not None else "")
    return _interface_fill_page(request, row, latest=latest, saved=saved, error=error,
                                lookup_prefill=lookup_html)


def _interface_fill_page(request: Request, row: dict[str, Any], *, latest: dict[str, Any] | None = None,
                         saved: str | None = None, error: str | None = None,
                         values: dict[str, Any] | None = None, status_code: int = 200,
                         lookup_prefill: str = ""):
    """Draw a department interface for filling.

    ``values`` overrides what the fields show, so a refused POST can come back with
    the text the person typed instead of the last saved entry or an empty form.

    ``lookup_prefill`` is the already-rendered lookup result for a full page opened
    with ``?lookup=`` -- the same fragment the htmx partial returns, handed to the
    renderer as text so the two paths cannot disagree.
    """
    descriptor = row["descriptor"]
    fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
    if values is None:
        values = _interface_values(fields, latest)
    # Same numbers the department workbench shows, computed for this one page: a
    # display module on the fill page reads the department aggregate, not the entry
    # the person is typing.
    with connection() as conn:
        rollup = api.build_department_aggregates(
            conn, row["org_unit_id"], descriptor=descriptor,
            interface_id=row.get("interface_id"))
    try:
        rendered = render_interface(descriptor, values=values, include_unbound=True,
                                    aggregates=rollup.get("aggregates") or {},
                                    lookup=lookup_prefill)
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
    api.require_app_scope(principal, "entries:write")
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
                  entry["entry_id"], f"在页面填报页填了一条「{entry['title']}」", entry)
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
                  f"在界面制作页新建了页面草稿「{name.strip()}」",
                  {"slug": slug.strip(), "version_no": row["version_no"]})
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
          member_id: str | None = None, login: str | None = None, expires: str | None = None,
          lead: str | None = None):
    """Guided pilot setup: department, position, member, assignment, grants, login link.

    `lead=1` is the receipt for `as_department_lead`: it is set only when the
    position the new member was seated on holds first-level page review, so the
    page never claims a grant the table does not have.
    """
    try:
        principal = _require(request, "workforce.grants", "manage")
        _require(request, "workforce.members", "manage")
        _require(request, "workforce.positions", "manage")
        _require(request, "workforce.assignments", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/setup", status_code=302)
    except _NotPermitted:
        return _denied(request, "组织与成员需要组织、成员、岗位、任职和授权管理权限。")
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
        "login_token": login, "login_expires": expires, "lead_granted": lead == "1",
        "member": _member(request),
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


#: The one extra right `as_department_lead` adds: signing off this department's pages.
PILOT_LEAD_GRANT = ("workforce.interfaces", "review")


def _grant_pilot_department_lead(conn, position_id: Any, actor_member_id: Any, now: str) -> bool:
    """Give the position first-level page review **inside its own department**.

    A fresh install has nobody who may sign a department's first review: the only
    `workforce.interfaces/review` grant sits on COMPANY/SYSADMIN, and that member
    is also the author of the page they just created, so the page can never go
    live (see docs/reports/20261103-onboarding-dryrun.md). The guided setup page
    therefore offers to make the member it creates that department's reviewer.

    **The row says `scope_type='all'` on purpose, and that is not a mistake to be
    "fixed" later by narrowing it.** `security.effective_grants` only honours
    `scope_type='all'` (see its SCOPE GAP comment), so an `org_unit` row here
    would grant nothing at all. The department boundary lives one layer up, in
    route code: `_review_refusal` lets a department reviewer sign only pages whose
    `org_unit_id` is among their own positions' departments, and refuses pages
    they authored themselves. So the grant record is wide and the behaviour is
    narrow -- do not read `all` here as "can sign the whole company".

    Returns whether the position holds that right afterwards -- true both when
    this call wrote the row and when an earlier call already had. The receipt the
    setup page shows is a statement about the grant table, not about who wrote it,
    so re-submitting the form for the same position shows the same receipt and
    writes nothing twice.
    """
    resource, action = PILOT_LEAD_GRANT
    existing = conn.execute(
        """SELECT 1 FROM oams.permission_grants
            WHERE subject_type = 'position' AND position_id = %s
              AND resource = %s AND action = %s
              AND scope_type = 'all' AND effect = 'allow' AND revoked_at IS NULL""",
        (position_id, resource, action)).fetchone()
    if existing is not None:
        return True
    api.insert_grant(conn, GrantCreate(
        subject_type="position", position_id=position_id, resource=resource, action=action,
        scope_type="all", valid_from=now,
        reason="pilot department lead from guided setup page"), actor_member_id)
    return True


@router.post("/setup/pilot")
def setup_pilot_member(request: Request, csrf: str = Form(...), display_name: str = Form(...),
                       employee_code: str = Form(default=""), position_id: UUID = Form(...),
                       as_department_lead: bool = Form(default=False)):
    """Create an active member, seat them on one position, grant reporting, hand back a login link.

    `as_department_lead` is optional and off by default; when set, the position
    also gets first-level page review within its own department only.
    """
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
    lead_granted = False
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
            if as_department_lead:
                lead_granted = _grant_pilot_department_lead(conn, position_id, principal["member_id"], now)
        token, expires_at = issue_session(str(member_id), "pilot-setup")
    except HTTPException as exc:
        if exc.status_code == 409:
            # 409 有两件事：员工编号被人用了，或者这个岗位已经坐着别人（一个岗位同时
            # 只能坐一个人）。两者以前都翻成「这个员工编号已经存在」——第二次把人往
            # 同一岗位放的人就去改一个没错的编号。判据是 api 侧给的稳定标记
            # （api.CONFLICT_HEADER），不是 detail 的措辞。
            conflict = (exc.headers or {}).get(api.CONFLICT_HEADER)
            if conflict == api.CONFLICT_POSITION_OCCUPIED:
                return RedirectResponse(url="/setup?error=pilot-position-taken", status_code=303)
            return RedirectResponse(url="/setup?error=pilot-duplicate", status_code=303)
        raise
    return RedirectResponse(url="/setup?saved=pilot&member_id=" + str(member_id)
                            + "&login=" + urllib.parse.quote(token)
                            + "&expires=" + urllib.parse.quote(expires_at.isoformat())
                            + ("&lead=1" if lead_granted else ""), status_code=303)


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



# ---------------------------------------------------------------------------
# 接入方（connected app）：页面那一半 + 给应用用的三个 JSON 出口
#
# 页面这一半只有一件事：人登录后看见「谁、要什么、到哪天」，勾一次允许。
# 应用那一半是 JSON：建草稿页、读条目、记条目；每个出口都要一张换来的票，
# 而且票上的范围必须在（api.require_app_scope），不能靠「有票就等于全权」。
# ---------------------------------------------------------------------------

CONNECT_FORM_PURPOSE = "oams:app-connect:v1"


def _api_principal(request: Request) -> dict[str, Any]:
    """Bearer 票或浏览器会话都收；这是 JSON 出口，不跳登录页。"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        row = principal_for_token(header[7:].strip())
        if row is None:
            raise HTTPException(status_code=401, detail="令牌无效、已过期或已被吊销")
        return row
    member = _member(request)
    if member is None:
        raise HTTPException(status_code=401, detail="需要会话令牌")
    return member


class _JsonForm:
    """把 JSON 对象当表单读：_read_entry_form 只要 getlist()。"""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def getlist(self, key: str) -> list[str]:
        value = self._data.get(key)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [str(value)]


def _json_error(detail: str, code: int = 422):
    return JSONResponse({"error": detail}, status_code=code)


def _app_name_for(conn, principal) -> str | None:
    """这张票是哪个应用换来的（接入方票才有；普通会话返回 None）。"""

    app_id = api.app_id_of(principal)
    if not app_id:
        return None
    row = conn.execute("SELECT name FROM oams.connected_apps WHERE app_id = %s",
                       (app_id,)).fetchone()
    return row["name"] if row is not None else None


@router.get("/connect/{code}", response_class=HTMLResponse)
def connect_app_page(request: Request, code: str, error: str | None = None):
    """人打开一次一码的地方：看清是谁、要什么，再决定点不点。"""
    member = _member(request)
    if member is None:
        return RedirectResponse(url=f"/login?next=/connect/{urllib.parse.quote(code)}", status_code=302)
    with connection() as conn:
        row = conn.execute(
            """SELECT c.*, p.name AS app_name, p.status AS app_status
                 FROM oams.app_codes c JOIN oams.connected_apps p USING (app_id)
                WHERE c.code = %s""", (code.strip(),)).fetchone()
        authorization = None
        if row is not None and row["authorization_id"] is not None:
            authorization = conn.execute(
                "SELECT * FROM oams.app_authorizations WHERE authorization_id = %s",
                (row["authorization_id"],)).fetchone()
    now = datetime.now(timezone.utc)
    reason = None
    if row is None:
        reason = "这个码不存在。"
    elif row["app_status"] != "active":
        reason = "这个应用已经被停用。"
    elif row["approved_at"] is None and row["expires_at"] <= now:
        reason = "这个码已经过期了，让应用重新取一个。"
    return TEMPLATES.TemplateResponse(request, "connect_app.html", {
        "member": member, "code": code, "app": row, "authorization": authorization,
        "reason": reason, "error": error, "now": now,
        "scopes": list(row["requested_scopes"] or []) if row is not None else [],
        "labels": api.APP_SCOPE_LABELS,
        "days_options": (1, 7, 30),
        "default_days": api.APP_DEFAULT_AUTH_DAYS,
        "csrf": _form_token(request, CONNECT_FORM_PURPOSE),
    }, status_code=404 if row is None else 200)


@router.post("/connect/{code}")
def connect_app_approve(request: Request, code: str, csrf: str = Form(...),
                        scope: list[str] = Form(default=[]), days: str = Form(default="7")):
    """人点「允许」：把范围写进 app_authorizations，并把码标成「已批准」。"""
    member = _member(request)
    back = f"/connect/{urllib.parse.quote(code.strip())}"
    if member is None:
        return RedirectResponse(url=f"/login?next={back}", status_code=302)
    _check_form_token(request, csrf, CONNECT_FORM_PURPOSE)
    try:
        want_days = int(days)
    except (TypeError, ValueError):
        want_days = api.APP_DEFAULT_AUTH_DAYS
    want_days = max(1, min(want_days, api.APP_MAX_AUTH_DAYS))
    now = datetime.now(timezone.utc)
    with connection() as conn:
        row = conn.execute(
            """SELECT c.*, p.name AS app_name, p.status AS app_status
                 FROM oams.app_codes c JOIN oams.connected_apps p USING (app_id)
                WHERE c.code = %s FOR UPDATE""", (code.strip(),)).fetchone()
        if row is None:
            return RedirectResponse(url=back + "?error=" + urllib.parse.quote("这个码不存在"), status_code=303)
        if row["app_status"] != "active":
            return RedirectResponse(url=back + "?error=" + urllib.parse.quote("这个应用已经被停用"),
                                    status_code=303)
        if row["approved_at"] is None and row["expires_at"] <= now:
            return RedirectResponse(url=back + "?error=" + urllib.parse.quote("这个码已经过期，让应用重新取一个"),
                                    status_code=303)
        requested = list(row["requested_scopes"] or [])
        chosen = [item for item in dict.fromkeys(scope or []) if item in requested and item in api.APP_SCOPES]
        valid_until = now + timedelta(days=want_days)
        existing = conn.execute(
            """SELECT * FROM oams.app_authorizations
                WHERE app_id = %s AND member_id = %s AND revoked_at IS NULL FOR UPDATE""",
            (row["app_id"], member["member_id"])).fetchone()
        if existing is None:
            authorization = conn.execute(
                """INSERT INTO oams.app_authorizations
                       (app_id, member_id, scopes, valid_until, approved_by, approved_at)
                   VALUES (%s, %s, %s, %s, %s, now()) RETURNING authorization_id""",
                (row["app_id"], member["member_id"], chosen, valid_until,
                 member["member_id"])).fetchone()
        else:
            # 同一个人再点一次允许 = 更新这一票，而不是叠出第二票：
            # 「我有几张票、哪一张在被用」这个问题没有意义。
            conn.execute(
                """UPDATE oams.app_authorizations
                      SET scopes = %s, valid_until = %s, approved_by = %s, approved_at = now()
                    WHERE authorization_id = %s""",
                (chosen, valid_until, member["member_id"], existing["authorization_id"]))
            authorization = existing
        conn.execute(
            """UPDATE oams.app_codes
                  SET approved_at = now(), approved_member_id = %s, authorization_id = %s
                WHERE code = %s AND approved_at IS NULL""",
            (member["member_id"], authorization["authorization_id"], row["code"]))
        conn.execute(
            """UPDATE oams.connected_apps
                  SET owner_member_id = COALESCE(owner_member_id, %s),
                      created_by = COALESCE(created_by, %s)
                WHERE app_id = %s""",
            (member["member_id"], member["member_id"], row["app_id"]))
        labels = "、".join(api.APP_SCOPE_LABELS.get(item, item) for item in chosen) or "（没有勾任何范围）"
        api.audit(conn, member["member_id"], "app.authorization.approved", "app_authorization",
                  authorization["authorization_id"],
                  f"通过 {row['app_name']} 代表 {member['display_name']} 允许：{labels}，"
                  f"有效期到 {valid_until.astimezone().strftime('%Y-%m-%d %H:%M')}",
                  {"app_id": str(row["app_id"]), "app_name": row["app_name"],
                   "scopes": chosen, "valid_until": valid_until.isoformat()},
                  metadata={"app_id": str(row["app_id"]), "app_name": row["app_name"]})
    return RedirectResponse(url=back + "?approved=1", status_code=303)


# ---------------------------------------------------------------------------
# 我的授权：允许过哪些应用、它们能做什么、到哪一天，以及一个「断开」
#
# 允许这一下是人在 /connect 里点的，收回也必须由人自己点得动。最早这一屏没有，
# 只在允许成功那页写「找管理员在审计里按应用名撤」——而审计是只读的，等于没退路。
# ---------------------------------------------------------------------------

APPS_PAGE_PURPOSE = "oams:apps-page:v1"


@router.get("/apps", response_class=HTMLResponse)
def my_app_authorizations(request: Request, disconnected: str | None = None):
    """我允许过的应用：范围用人话写、有效期写日期，每条一个「断开」。"""

    member = _member(request)
    if member is None:
        return RedirectResponse(url="/login?next=/apps", status_code=302)
    with connection() as conn:
        rows = conn.execute(
            """SELECT a.authorization_id, a.app_id, a.scopes, a.valid_until, a.approved_at,
                      a.last_token_issued_at, p.name AS app_name
                 FROM oams.app_authorizations a JOIN oams.connected_apps p USING (app_id)
                WHERE a.member_id = %s AND a.revoked_at IS NULL AND a.valid_until > now()
                ORDER BY a.approved_at DESC""", (member["member_id"],)).fetchall()
    return TEMPLATES.TemplateResponse(request, "app_authorizations.html", {
        "member": member,
        "authorizations": rows,
        "labels": api.APP_SCOPE_LABELS,
        "csrf": _form_token(request, APPS_PAGE_PURPOSE),
        "disconnected": disconnected,
    })


@router.post("/apps/{authorization_id}/revoke")
def my_app_authorization_revoke(request: Request, authorization_id: UUID, csrf: str = Form(...)):
    """断开一个应用：与 JSON 出口共用 api.revoke_authorization，票当场吊销。"""

    member = _member(request)
    if member is None:
        return RedirectResponse(url="/login?next=/apps", status_code=302)
    _check_form_token(request, csrf, APPS_PAGE_PURPOSE)
    with connection() as conn:
        killed = api.revoke_authorization(conn, member["member_id"], authorization_id,
                                          "在「我的授权」里断开")
    return RedirectResponse(url=f"/apps?disconnected={killed}", status_code=303)


@router.post("/v1/interfaces", status_code=201)
async def api_create_interface(request: Request):
    """应用建一张草稿页：和页面里「新建页面」同一个校验，只是出口换成 JSON。"""
    principal = _api_principal(request)
    try:
        payload = await request.json()
    except Exception:
        return _json_error("请求体必须是 JSON", 400)
    if not isinstance(payload, dict):
        return _json_error("请求体必须是 JSON 对象", 400)
    slug = str(payload.get("slug") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not slug or not name:
        return _json_error("slug 和 name 都是必填的")
    descriptor = payload.get("descriptor")
    if isinstance(descriptor, str):
        try:
            descriptor = json.loads(descriptor)
        except ValueError:
            return _json_error("descriptor 不是合法的 JSON")
    if not isinstance(descriptor, dict):
        return _json_error("descriptor 要是一个 JSON 对象")
    position_id = str(payload.get("position_id") or "").strip()
    try:
        target = UUID(position_id) if position_id else None
    except ValueError:
        return _json_error("position_id 不是合法的编号")
    with connection() as conn:
        if not (has_permission(conn, principal["member_id"], "workforce.reports", "read")
                and has_permission(conn, principal["member_id"], "workforce.reports", "submit")):
            raise HTTPException(status_code=403, detail="建页面需要 workforce.reports 读写权限")
        api.require_app_scope(principal, "interfaces:draft")
        # 部门线索的取法：请求体显式给的 > descriptor 顶层 > descriptor.meta。
        # 顶层那条最要紧：descriptor 自己就写着 org_unit_code，调用方不该被迫再抄一遍。
        org_hint = payload.get("org_unit_id") or payload.get("org_unit_code")
        if not org_hint:
            org_hint = descriptor.get("org_unit_code")
        if not org_hint:
            org_hint = (descriptor.get("meta") or {}).get("org_unit_code")
        if not org_hint:
            return _json_error(
                "没说清要建哪个部门的页面：请在请求体里给 org_unit_code（或 org_unit_id），"
                "或写进 descriptor 的顶层 org_unit_code。")
        # 先认出部门，再判权限：「认不出这个部门」是请求写错了，不是「你没有权限」。
        row = None
        if isinstance(org_hint, str) and org_hint.strip():
            row = conn.execute(
                "SELECT org_unit_id FROM oams.org_units WHERE org_code = %s AND status = 'active'",
                (org_hint.strip(),)).fetchone()
        if row is None:
            try:
                row = conn.execute(
                    """SELECT org_unit_id FROM oams.org_units
                        WHERE org_unit_id = %s AND status = 'active'""",
                    (UUID(str(org_hint)),)).fetchone()
            except (ValueError, TypeError):
                row = None
        if row is None:
            return _json_error(f"没有这个部门：{org_hint}——请给部门的 org_code（如 IT）或 org_unit_id")
        unit = _resolve_department(conn, principal, row["org_unit_id"])
        if unit is None:
            raise HTTPException(status_code=403, detail="你不在这个部门的范围内")
        org_unit_id = unit["org_unit_id"]
        codes = {row["org_code"] for row in conn.execute(
            "SELECT org_code FROM oams.org_units WHERE status = 'active'").fetchall()}
        known_positions = {row["position_code"] for row in conn.execute(
            "SELECT position_code FROM oams.positions WHERE status = 'active'").fetchall()}
        problems = validate_descriptor(descriptor, known_org_unit_codes=codes,
                                       known_position_codes=known_positions)
        if problems:
            return JSONResponse({"error": "描述符没通过校验", "problems": problems}, status_code=422)
        if target is not None:
            owned = conn.execute(
                "SELECT 1 FROM oams.positions WHERE position_id = %s AND org_unit_id = %s AND status = 'active'",
                (target, org_unit_id)).fetchone()
            if owned is None:
                return _json_error("position_id 不是这个部门在用的岗位")
        open_version = conn.execute(
            """SELECT interface_id FROM oams.interfaces
                WHERE org_unit_id = %s AND slug = %s AND status IN ('draft', 'pending')
                ORDER BY version_no DESC LIMIT 1""", (org_unit_id, slug)).fetchone()
        if open_version is not None:
            return JSONResponse({"error": "这个页面已经有一版在改或待审核",
                                 "interface_id": str(open_version["interface_id"])}, status_code=409)
        live = conn.execute(
            """SELECT interface_id FROM oams.interfaces
                WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (org_unit_id, slug)).fetchone()
        version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 FROM oams.interfaces
                WHERE org_unit_id = %s AND slug = %s""", (org_unit_id, slug)).fetchone()["?column?"]
        row = conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    position_id, version_no, based_on, created_by, updated_by)
               VALUES (%s, %s, %s, 'draft', 'department', %s, %s, %s, %s, %s, %s, %s)
               RETURNING interface_id""",
            (org_unit_id, slug, name, descriptor.get("schema_version", "0.1"), Jsonb(descriptor),
             target, version, live["interface_id"] if live is not None else None,
             principal["member_id"], principal["member_id"])).fetchone()
        app_name = _app_name_for(conn, principal) or "接入方"
        api.audit(conn, principal["member_id"], "interface.created", "interface", row["interface_id"],
                  f"通过 {app_name} 代表 {principal.get('display_name') or '这个人'} 新建了页面草稿"
                  f"「{name}」",
                  {"slug": slug, "version_no": version,
                   "app_id": api.app_id_of(principal)},
                  metadata={"app_id": api.app_id_of(principal), "slug": slug})
    return JSONResponse({
        "interface_id": str(row["interface_id"]),
        "org_unit_id": str(org_unit_id),
        "slug": slug,
        "name": name,
        "status": "draft",
        "version_no": version,
        "url": f"/departments/{org_unit_id}/{row['interface_id']}",
    }, status_code=201)


@router.post("/v1/interfaces/{interface_id}/submit")
def api_submit_interface(request: Request, interface_id: UUID):
    """应用把一张草稿页送审：和工作台「提交送审」同一条规则，出口换成 JSON。

    送审本身不认作者（工作台表单也不认）：部门里任何一个有填报权的人都能把草稿
    送上去。审核那一侧同样不看作者：部门主管签自己部门交的页（包括自己交的），
    系统管理员签任何页。
    """
    principal = _api_principal(request)
    with connection() as conn:
        page = conn.execute("SELECT * FROM oams.interfaces WHERE interface_id = %s",
                            (interface_id,)).fetchone()
        if page is None:
            raise HTTPException(status_code=404, detail="没有这个页面")
        if not (has_permission(conn, principal["member_id"], "workforce.reports", "read")
                and has_permission(conn, principal["member_id"], "workforce.reports", "submit")):
            raise HTTPException(status_code=403, detail="送审需要 workforce.reports 读写权限")
        api.require_app_scope(principal, "interfaces:draft")
        if _resolve_department(conn, principal, page["org_unit_id"]) is None:
            raise HTTPException(status_code=403, detail="你不在这个部门的范围内")
        status_now = page["status"]
        if status_now == "pending":
            return _json_error("这个页面已经提交，正在等审核", 409)
        if status_now == "active":
            return _json_error("这个页面已经启用，无需再次提交", 409)
        if status_now == "archived":
            current = _current_version_id(conn, page["org_unit_id"], page["slug"])
            return JSONResponse({
                "error": "这一版已归档，不能送审。要改就改现在的版本。",
                "current_interface_id": str(current) if current is not None else None,
            }, status_code=409)
        if not _is_page_author(page, principal):
            return _json_error("这一页不是你建的，别替别人送审——让作者自己送", 403)
        conn.execute(
            """UPDATE oams.interfaces
                  SET status = 'pending', review_stage = 'department', review_note = NULL,
                      updated_at = now()
                WHERE interface_id = %s""", (interface_id,))
        app_name = _app_name_for(conn, principal)
        who = principal.get("display_name") or "这个人"
        reason = (f"通过 {app_name} 代表 {who} 提交了页面送审，等公司审"
                  if app_name else "提交了页面送审，等公司审")
        api.audit(conn, principal["member_id"], "interface.review_requested", "interface",
                  interface_id, reason, {"from": status_now, "to": "pending"},
                  metadata={"app_id": api.app_id_of(principal), "slug": page["slug"]})
    return JSONResponse({
        "interface_id": str(interface_id),
        "org_unit_id": str(page["org_unit_id"]),
        "slug": page["slug"],
        "name": page["name"],
        "status": "pending",
        "review_stage": "department",
        "version_no": page["version_no"],
        "author_member_id": str(page["created_by"]) if page["created_by"] else None,
        "url": f"/departments/{page['org_unit_id']}/{interface_id}",
    })


@router.post("/v1/interfaces/{interface_id}/review")
async def api_review_interface(request: Request, interface_id: UUID):
    """接入方替人签一版页面：和工作台那两颗按钮同一条判据，出口换成 JSON。

    审核是**人**的动作，所以这张票要有 `interfaces:review`（人在允许时勾的那一项），
    并且它代表的那个人本来就够审核权——应用签得不会比这个人多。谁签哪一级由
    `_review_refusal` 一个函数说了算，这里不另立规矩。
    """
    principal = _api_principal(request)
    try:
        payload = await request.json()
    except Exception:
        return _json_error("请求体必须是 JSON", 400)
    if not isinstance(payload, dict):
        return _json_error("请求体必须是 JSON 对象", 400)
    decision = str(payload.get("decision") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    reason_code = str(payload.get("reason_code") or "").strip() or None
    if decision not in ("approve", "reject"):
        return _json_error("decision 只能是 approve 或 reject")
    if decision == "reject" and not note:
        return _json_error("退回必须写明原因")
    if reason_code is not None and not review_rules.is_rejection_code(reason_code):
        return _json_error("未知的退回原因类型")
    api.require_app_scope(principal, "interfaces:review")
    with connection() as conn:
        powers = _review_powers(conn, principal)
        page = conn.execute(
            """SELECT interface_id, org_unit_id, name, slug, status, version_no, descriptor,
                      review_stage, updated_by, based_on
               FROM oams.interfaces WHERE interface_id = %s FOR UPDATE""",
            (interface_id,)).fetchone()
        if page is None:
            raise HTTPException(status_code=404, detail="没有这个页面")
        refusal = _review_refusal(powers, page)
        if refusal is not None:
            raise HTTPException(status_code=403, detail=refusal)
        acting = _acting_stage(powers, page)
        if decision == "reject":
            _reject_page(conn, principal, page, note, reason_code, acting)
            saved = None
        else:
            saved, failed = _approve_page(conn, principal, page, note, acting)
            if failed:
                return _json_error(failed, 409)
        refreshed = conn.execute(
            """SELECT status, review_stage, version_no FROM oams.interfaces
                WHERE interface_id = %s""", (interface_id,)).fetchone()
    return JSONResponse({
        "interface_id": str(interface_id),
        "decision": decision,
        "saved": saved,
        "status": refreshed["status"],
        "review_stage": refreshed["review_stage"],
        "version_no": refreshed["version_no"],
        "acting": acting,
        "url": f"/departments/{page['org_unit_id']}/{interface_id}",
    })


def _require_any_app_scope(principal, *scopes):
    """应用票只要命中其中任意一个范围就放行（普通会话照旧按人判权）。"""
    if api.app_id_of(principal) is None:
        return None
    authorization = api.live_app_authorization(principal)
    if authorization is None:
        raise HTTPException(status_code=403, detail="这张票的授权已被撤销或已到期")
    allowed = authorization["scopes"] or []
    if not any(scope in allowed for scope in scopes):
        wanted = "或".join(f"「{api.APP_SCOPE_LABELS.get(scope, scope)}」" for scope in scopes)
        raise HTTPException(status_code=403, detail=f"这个接入方没有被允许{wanted}")
    return authorization


@router.get("/v1/interfaces/{interface_id}")
def api_get_interface(request: Request, interface_id: UUID):
    """回读一页现在的状态：送审之后用它确认 status 真变成了 pending。

    「我把页面送审了」不是证据，回读到的 status 才是——外部 AI 自报完成不算数，
    所以给它一个不靠猜的出口。
    """
    principal = _api_principal(request)
    with connection() as conn:
        page = _page_for_app(conn, principal, interface_id)
        _require_any_app_scope(principal, "interfaces:draft", "entries:read:dept")
        versions = conn.execute(
            """SELECT interface_id, version_no, status FROM oams.interfaces
                WHERE org_unit_id = %s AND slug = %s ORDER BY version_no DESC""",
            (page["org_unit_id"], page["slug"])).fetchall()
    return {
        "interface_id": str(page["interface_id"]),
        "org_unit_id": str(page["org_unit_id"]),
        "slug": page["slug"],
        "name": page["name"],
        "status": page["status"],
        "review_stage": page["review_stage"],
        "version_no": page["version_no"],
        "author_member_id": str(page["created_by"]) if page["created_by"] else None,
        "updated_at": page["updated_at"].isoformat() if page["updated_at"] else None,
        "versions": [{"interface_id": str(row["interface_id"]), "version_no": row["version_no"],
                      "status": row["status"]} for row in versions],
        "url": f"/departments/{page['org_unit_id']}/{page['interface_id']}",
    }


def _page_for_app(conn, principal, interface_id):
    """应用票能碰的页面：必须是这个人在部门范围内看得见的某一版。"""
    page = conn.execute("SELECT * FROM oams.interfaces WHERE interface_id = %s",
                        (interface_id,)).fetchone()
    if page is None:
        raise HTTPException(status_code=404, detail="没有这个页面")
    # 部门内照旧；部门外只认页面自己共享出去的那张表（页面设置里授权的部门）。
    if not can_access_org_unit(conn, principal["member_id"], page["org_unit_id"]):
        if not security.can_view_interface(conn, principal["member_id"],
                                          page["org_unit_id"], page["slug"]):
            raise HTTPException(
                status_code=403,
                detail="这个部门不在你的范围内：你的岗位和被授权的部门都不含它")
    return page


@router.get("/v1/interfaces/{interface_id}/entries")
def api_list_entries(request: Request, interface_id: UUID, limit: int = 200):
    principal = _api_principal(request)
    limit = max(1, min(limit, 500))
    with connection() as conn:
        if not has_permission(conn, principal["member_id"], "workforce.reports", "read"):
            raise HTTPException(status_code=403, detail="读条目需要 workforce.reports 读权限")
        api.require_app_scope(principal, "entries:read:dept")
        page = _page_for_app(conn, principal, interface_id)
        rows = conn.execute(
            """SELECT e.entry_id, e.interface_id, e.title, e.status, e.note, e.evidence,
                      e.progress_value, e.reported_at, e.status_entry, e.entry_mode, e.extra,
                      v.version_no
                 FROM oams.interface_entries e JOIN oams.interfaces v USING (interface_id)
                WHERE v.org_unit_id = %s AND v.slug = %s AND e.status_entry = 'active'
                ORDER BY e.reported_at DESC LIMIT %s""",
            (page["org_unit_id"], page["slug"], limit)).fetchall()
    status_map = (page["descriptor"] or {}).get("status_map", {})
    status_field = next((f.get("key") for f in (page["descriptor"] or {}).get("fields", [])
                         if isinstance(f, dict) and f.get("map_to") == "status"), None)
    entries = []
    for row in rows:
        item = {key: (str(row[key]) if key == "entry_id" else row[key])
                for key in ("entry_id", "interface_id", "title", "note", "evidence",
                            "progress_value", "status_entry", "entry_mode")}
        item["status"] = row["status"]
        item["status_word"] = ((row["extra"] or {}).get(status_field) if status_field else None) \
            or next((word for word, state in status_map.items() if state == row["status"]), None) \
            or row["status"]
        item["version_no"] = row["version_no"]
        item["reported_at"] = row["reported_at"].isoformat() if row["reported_at"] else None
        item["extra"] = row["extra"] or {}
        entries.append(item)
    return {"interface_id": str(interface_id), "slug": page["slug"], "count": len(entries),
            "entries": entries}


@router.post("/v1/interfaces/{interface_id}/entries", status_code=201)
async def api_create_entry(request: Request, interface_id: UUID):
    principal = _api_principal(request)
    try:
        payload = await request.json()
    except Exception:
        return _json_error("请求体必须是 JSON", 400)
    if not isinstance(payload, dict):
        return _json_error("请求体必须是 JSON 对象", 400)
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    with connection() as conn:
        if not (has_permission(conn, principal["member_id"], "workforce.reports", "read")
                and has_permission(conn, principal["member_id"], "workforce.reports", "submit")):
            raise HTTPException(status_code=403, detail="填报条目需要 workforce.reports 读写权限")
        api.require_app_scope(principal, "entries:write")
        page = conn.execute("SELECT * FROM oams.interfaces WHERE interface_id = %s FOR UPDATE",
                            (interface_id,)).fetchone()
        if page is None:
            raise HTTPException(status_code=404, detail="没有这个页面")
        if not can_access_org_unit(conn, principal["member_id"], page["org_unit_id"]):
            if not security.can_fill_interface(conn, principal["member_id"],
                                              page["org_unit_id"], page["slug"]):
                raise HTTPException(
                    status_code=403,
                    detail="这一页对你只读：你的部门能看，不能填；填写由页面所属部门负责")
        if page["status"] == "archived":
            return _json_error("这一版已归档，不能再往里写；先打开这个页面现在的版本", 409)
        descriptor = page["descriptor"] or {}
        fields = [field for field in descriptor.get("fields", [])
                  if isinstance(field, dict) and field.get("key")]
        standard, extra, problem = _read_entry_form(fields, descriptor.get("status_map", {}),
                                                    _JsonForm(values))
        if problem is not None:
            return _json_error(problem)
        mode = _entry_mode_for(page["status"])
        columns = dict(standard)
        columns["extra"] = Jsonb(extra)
        columns["reported_by"] = principal["member_id"]
        columns["entry_mode"] = mode
        keys = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        entry = conn.execute(
            "INSERT INTO oams.interface_entries (interface_id, " + keys + ") VALUES (%s, "
            + placeholders + ") RETURNING entry_id, title, status, note, evidence, reported_at",
            (interface_id, *columns.values())).fetchone()
        app_name = _app_name_for(conn, principal) or "接入方"
        wrote = "本页现行的条目" if mode == "live" else "草稿版里的条目"
        api.audit(conn, principal["member_id"], "interface_entry.created", "interface_entry",
                  entry["entry_id"],
                  f"通过 {app_name} 代表 {principal.get('display_name') or '这个人'} 记了一条"
                  f"「{entry['title']}」（{wrote}）",
                  dict(entry, app_id=api.app_id_of(principal)),
                  metadata={"app_id": api.app_id_of(principal)})
    return JSONResponse({
        "entry_id": str(entry["entry_id"]),
        "interface_id": str(interface_id),
        "title": entry["title"],
        "status": entry["status"],
        "entry_mode": mode,
        "status_entry": "active",
        "extra": extra,
    }, status_code=201)
