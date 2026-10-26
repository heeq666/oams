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
import urllib.parse
from datetime import datetime, timezone
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

from workhub import admin_labels, api, funnel, metrics, review as review_rules
from workhub.config import settings
from workhub.datacenter import DataCenter, DataCenterNotConfigured, build_datacenter
from workhub.db import connection
from workhub.descriptor import validate_descriptor
from workhub.render import RenderError, render_interface, render_read_only
from workhub.schemas import (AssignmentCreate, GrantCreate, MemberCreate, OrgUnitCreate, PositionCreate,
                             ReportCreate, SpaceUpsert, WorkItemCreate)
from workhub.security import (DEV_WORKER_COOKIE, active_org_unit_ids, dev_autologin_target, dev_identities,
                              dev_principal, has_company_scope, has_permission, issue_session,
                              principal_from_request, principal_for_token, require_org_unit_access,
                              revoke_session)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")


def _display_time(value: datetime | None, fmt: str = "%m-%d %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TZ).strftime(fmt)


TEMPLATES.env.filters["dt"] = _display_time
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
ENTRY_LINK_PURPOSE = "workhub:entry-link:v1"


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
               FROM workhub.positions p
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


def _denied(request: Request, message: str):
    return TEMPLATES.TemplateResponse(request, "denied.html",
                                      {"message": message, "member": _member(request)}, status_code=403)


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


def _form_token(request: Request, purpose: str = "workhub:form:v1") -> str:
    """Bind each POST form to the signed-in session, so a stale or forged token fails."""
    token = request.cookies.get(settings.web_session_cookie, "")
    return hmac.new(token.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _report_csrf(request: Request) -> str:
    return _form_token(request, "workhub:report-form:v1")


def _check_form_token(request: Request, supplied: str, purpose: str) -> None:
    if not hmac.compare_digest(supplied or "", _form_token(request, purpose)):
        raise HTTPException(status_code=403, detail="invalid form token")


class _NotSignedIn(Exception):
    pass


class _NotPermitted(Exception):
    pass


# --------------------------------------------------------------------------- #
# Company metrics (platform funnel, contract section 2 and 7)
#
# The numbers do not live here: workhub.funnel computes them from the product data
# centre, which is a machine on the office LAN and is routinely unreachable from
# wherever this process happens to run. That makes "the data centre is down" an
# ordinary state rather than an incident, so every function below is total: it
# returns a panel or an explanation and never raises. A missing sidebar must never
# take a page down with it.
# --------------------------------------------------------------------------- #

METRICS_UNAVAILABLE_PREFIX = "指标暂不可用"

#: How long a page render will wait for the data centre. The default client
#: timeout (20s) is right for a batch job and wrong for a page: an unreachable
#: LAN host would hold the whole board open for a panel that is not the page's
#: subject. Failing fast into 「指标暂不可用」 is the better trade.
METRICS_HTTP_TIMEOUT = 6.0


def _metric_reason(exc: Exception) -> str:
    """One short sentence a manager can act on, never a stack trace."""
    if isinstance(exc, DataCenterNotConfigured):
        return "商品数据中心地址未配置（环境变量 WORKHUB_DATACENTER_URL）"
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def _page_datacenter(dc: DataCenter | None = None) -> DataCenter:
    """The data centre as a *page* wants it: present or failing fast.

    Only the timeout differs from build_datacenter's default, and it is narrowed
    rather than configured because the default (20s) is tuned for a batch job
    that is allowed to wait. On a page, a firewalled LAN host would otherwise
    stall the board for twenty seconds before falling back to the same message.
    """
    centre = build_datacenter(dc)
    if dc is None and hasattr(centre, "timeout"):
        centre.timeout = min(float(getattr(centre, "timeout") or 0) or METRICS_HTTP_TIMEOUT,
                             METRICS_HTTP_TIMEOUT)
    return centre


def _report_wave(dc: DataCenter) -> str | None:
    """The configured wave, else the data centre's first one.

    Configuration wins so an operator can pin the home page to the season being
    reported on. The first wave is the fallback because a wave string cannot be
    hard-coded: it changes every season, and a stale one reports zeros without
    anything looking broken.
    """
    configured = (settings.default_wave or "").strip()
    if configured:
        return configured
    waves = dc.list_waves() or []
    return waves[0] if waves else None


def _metric_rows(values: dict[str, Any]) -> list[dict[str, Any]]:
    """The six home cards, already formatted, in metrics.HOME_DEFAULT order."""
    rows: list[dict[str, Any]] = []
    for key in metrics.HOME_DEFAULT:
        spec = metrics.get_metric(key)
        value = values.get(key)
        rows.append({
            "key": key,
            "label": spec.name if spec is not None else key,
            "value": value,
            # One formatter for the whole product: the API, the HTML and any
            # future export must not each round money their own way.
            "formatted": funnel.format_metric(key, value),
            "unit": spec.unit if spec is not None else funnel.METRIC_UNITS.get(key, ""),
        })
    return rows


def company_metrics_panel(dc: DataCenter | None = None) -> dict[str, Any]:
    """The six home page cards. Never raises; see the block comment above."""
    try:
        centre = _page_datacenter(dc)
        wave = _report_wave(centre)
        values = funnel.compute_funnel(centre, wave=wave)
    except Exception as exc:  # noqa: BLE001 - an unreachable data centre is normal here
        return {"available": False, "reason": _metric_reason(exc), "wave": None, "metrics": []}
    return {"available": True, "reason": None, "wave": wave, "metrics": _metric_rows(values)}


def department_funnel_chain(dc: DataCenter | None = None) -> dict[str, Any]:
    """有开发记录 → 已出样 → 有核价记录 → 核价完成 → 已下单.

    The two "has a record" stages count *rows that exist*, which is not a funnel
    metric, so they are derived here from the same normalised payload the funnel
    already reads instead of widening funnel.py for a display-only number.

    They are deliberately company wide: the data centre carries no department
    dimension on a style yet, so filtering by department would be an invention.
    """
    try:
        centre = _page_datacenter(dc)
        wave = _report_wave(centre)
        products = centre.list_products(wave=wave) or []
        styles = (centre.list_development(wave=wave) or {}).get("styles") or {}
        values = funnel.compute_funnel(centre, wave=wave)
    except Exception as exc:  # noqa: BLE001 - see company_metrics_panel
        return {"available": False, "reason": _metric_reason(exc), "wave": None, "stages": []}
    # Restricted to styles that are in this wave's product list, so the first
    # stage can never exceed the number of styles the shop actually has.
    codes = {str(product.get("style_code")) for product in products if product.get("style_code")}
    recorded = sum(1 for code, entry in styles.items() if entry and code in codes)
    priced_records = sum(
        1 for code, entry in styles.items()
        if entry and code in codes and (entry.get("pricing_status") or "").strip()
    )
    stages = [
        {"key": "development_recorded", "label": "有开发记录", "count": recorded},
        {"key": "funnel.sample_done", "label": "已出样", "count": values.get("funnel.sample_done")},
        {"key": "pricing_recorded", "label": "有核价记录", "count": priced_records},
        {"key": "funnel.priced", "label": "核价完成", "count": values.get("funnel.priced")},
        {"key": "funnel.ordered", "label": "已下单", "count": values.get("funnel.ordered")},
    ]
    return {"available": True, "reason": None, "wave": wave, "stages": stages}


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
        return _login(request, error=f"免登录账号「{target}」不存在或已离职，请改 WORKHUB_DEV_AUTOLOGIN 或去掉它。", next_url=next_url)
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
        return _login(request, error="开发阶段免登录已开启（WORKHUB_DEV_AUTOLOGIN）：退出后会立刻自动登录回来。", next_url="/board")
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(settings.web_session_cookie, path="/")
    return response


@router.post("/dev/identity")
def dev_switch_identity(request: Request, code: str = Form(default=""), next: str = Form(default="/board")):
    """Design-stage identity switch: the badge writes the chosen person to a cookie.

    Only reachable while WORKHUB_DEV_AUTOLOGIN is on -- otherwise it 404s, so this
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
        # Clearing the cookie falls back to whatever WORKHUB_DEV_AUTOLOGIN says.
        response.delete_cookie(DEV_WORKER_COOKIE, path="/")
        return response
    with connection() as conn:
        row = conn.execute(
            "SELECT employee_code FROM workhub.workforce_members "
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


def _read_entry_form(fields, status_map, form):
    """Turn posted keys into standard columns plus the department's own extras."""
    standard = {}
    extra = {}
    for field in fields:
        key = field.get("key")
        if not key:
            continue
        raw = (form.get(key) or "").strip()
        map_to = field.get("map_to")
        if field.get("required") and not raw:
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
            """SELECT org_unit_id, org_code, name FROM workhub.org_units
               WHERE status = 'active' AND unit_type <> 'company' ORDER BY name""").fetchall()
    scoped = list(active_org_unit_ids(conn, member_id))
    if not scoped:
        return []
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM workhub.org_units
           WHERE status = 'active' AND org_unit_id = ANY(%s) ORDER BY name""", (scoped,)).fetchall()


def _department_pages(conn, org_unit_id):
    """Every page of a department, drafts included: a department must see what it is building."""
    return conn.execute(
        """SELECT i.interface_id, i.name, i.status, i.version_no, i.visibility,
                  (SELECT count(*) FROM workhub.interface_entries e
                    WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
           FROM workhub.interfaces i
           WHERE i.org_unit_id = %s AND i.status <> 'archived'
           ORDER BY (i.status = 'active') DESC, i.name""", (org_unit_id,)).fetchall()


def _resolve_department(conn, principal, org_unit_id):
    """Return the department row, or None when this member has no business there."""
    if not has_company_scope(conn, principal["member_id"]):
        if org_unit_id not in active_org_unit_ids(conn, principal["member_id"]):
            return None
    return conn.execute(
        """SELECT org_unit_id, org_code, name FROM workhub.org_units
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
        "funnel_chain": department_funnel_chain(),
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "workhub:interface-form:v1")})


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
            """SELECT position_id, position_code, title FROM workhub.positions
               WHERE org_unit_id = %s AND status = 'active' ORDER BY position_code""",
            (org_unit_id,)).fetchall()
        pages = _department_pages(conn, org_unit_id)
        found = _visible_departments(conn, principal["member_id"])
        counts = {row["org_unit_id"]: _department_pages(conn, row["org_unit_id"]) for row in found}
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    return TEMPLATES.TemplateResponse(request, "department_new_page.html", {
        "unit": unit, "departments": departments, "pages": pages,
        "current": None, "page": None, "positions": positions,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "workhub:page-form:v1")})


@router.post("/departments/{org_unit_id}/new")
def department_create_page(request: Request, org_unit_id: UUID, csrf: str = Form(...),
                           slug: str = Form(...), name: str = Form(...),
                           position_id: str = Form(default=""), descriptor_text: str = Form(...)):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/departments", status_code=302)
    except _NotPermitted:
        return _denied(request, "制作部门页面需要 workforce.reports 读写权限。")
    _check_form_token(request, csrf, "workhub:page-form:v1")
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
            "SELECT org_code FROM workhub.org_units WHERE status = 'active'").fetchall()}
        known_positions = {row["position_code"] for row in conn.execute(
            "SELECT position_code FROM workhub.positions WHERE status = 'active'").fetchall()}
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
            """SELECT interface_id FROM workhub.interfaces
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
            """SELECT interface_id, version_no FROM workhub.interfaces
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (org_unit_id, slug.strip())).fetchone()
        version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 FROM workhub.interfaces
               WHERE org_unit_id = %s AND slug = %s""", (org_unit_id, slug.strip())).fetchone()["?column?"]
        # A page a department just wrote starts unapproved: it must be seen and
        # filled inside the department before it can claim to speak for the company.
        row = conn.execute(
            """INSERT INTO workhub.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    position_id, version_no, based_on, created_by, updated_by)
               VALUES (%s, %s, %s, 'draft', 'department', %s, %s, %s, %s, %s, %s, %s)
               RETURNING interface_id""",
            (org_unit_id, slug.strip(), name.strip(), descriptor.get("schema_version", "0.1"),
             Jsonb(descriptor), position, version,
             live["interface_id"] if live is not None else None,
             principal["member_id"], principal["member_id"])).fetchone()
        api.audit(conn, principal["member_id"], "interface.created", "interface", row["interface_id"],
                  "created inside department workbench", {"slug": slug.strip(), "version_no": version})
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
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        page = conn.execute(
            """SELECT * FROM workhub.interfaces
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
            """SELECT * FROM workhub.interface_entries
               WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
               ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
        # A draft, or a page under review, does not replace the live one: name the
        # version still serving the company, or the department will believe the
        # screen it is editing is the screen the company sees.
        live_version = None
        if page["status"] != "active":
            live_version = conn.execute(
                """SELECT interface_id, version_no, updated_at FROM workhub.interfaces
                   WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
                (org_unit_id, page["slug"])).fetchone()
    descriptor = page["descriptor"]
    fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
    try:
        rendered = render_interface(descriptor, values=_interface_values(fields, latest))
    except RenderError as exc:
        return _denied(request, "这个页面的描述未通过校验，暂不能填写：" + str(exc))
    departments = [dict(unit=row, pages=counts.get(row["org_unit_id"], [])) for row in found]
    return TEMPLATES.TemplateResponse(request, "department_workbench.html", {
        "unit": unit, "departments": departments, "pages": pages,
        "current": page, "page": page, "rendered": rendered, "recent": recent,
        "preview": page["status"] != "active", "draft_mode": page["status"] != "active",
        "page_status_label": {"draft": "待提交审核", "pending": "审核中"}.get(page["status"], page["status"]),
        "live_version": live_version,
        "review_code_label": (review_rules.rejection_label(page.get("review_code"))
                              if page.get("review_code") else None),
        # The same five stages as the company page: a department reads the funnel
        # it feeds, from the one source of truth, rather than a private copy.
        "funnel_chain": department_funnel_chain(),
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
        "work_item_csrf": _form_token(request, "workhub:work-item-form:v1"),
        "can_manage_work_items": can_manage_work_items,
        # The page holds two different forms (fill it in, submit it for review),
        # so it carries a token for each; a token minted for one must not pass the other.
        "csrf": _form_token(request, "workhub:interface-form:v1"),
        "review_csrf": _form_token(request, "workhub:page-form:v1")})


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
    _check_form_token(request, csrf, "workhub:interface-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        page = conn.execute(
            """SELECT * FROM workhub.interfaces
               WHERE interface_id = %s AND org_unit_id = %s AND status <> 'archived'""",
            (interface_id, org_unit_id)).fetchone()
        if page is None:
            return _denied(request, "这个页面不存在或不属于该部门。")
        descriptor = page["descriptor"]
        fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
        form = await request.form()
        standard, extra, problem = _read_entry_form(fields, descriptor.get("status_map", {}), form)
        if problem is not None:
            return RedirectResponse(url=base + "?error=" + urllib.parse.quote(problem), status_code=303)
        mode = _entry_mode_for(page["status"])
        columns = dict(standard)
        columns["extra"] = Jsonb(extra)
        columns["reported_by"] = principal["member_id"]
        columns["entry_mode"] = mode
        keys = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        entry = conn.execute(
            "INSERT INTO workhub.interface_entries (interface_id, " + keys + ") VALUES (%s, " + placeholders + ")"
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
                """SELECT entry_id, interface_id, title FROM workhub.interface_entries
                   WHERE entry_id = %s AND interface_id = %s""", (entry_id, interface_id)).fetchone()
            if entry is None:
                return _denied(request, "找不到这条汇报记录。")
            if target is not None:
                # The work item must exist and sit inside a department this member
                # may report on; otherwise the form would link across departments.
                item = conn.execute(
                    """SELECT w.work_item_id, w.org_unit_id FROM workhub.work_items w
                       JOIN workhub.positions p USING (position_id)
                       WHERE w.work_item_id = %s AND p.org_unit_id = %s""",
                    (target, org_unit_id)).fetchone()
                if item is None:
                    return RedirectResponse(url=base + "?error=work-item-invalid", status_code=303)
            conn.execute(
                "UPDATE workhub.interface_entries SET work_item_id = %s WHERE entry_id = %s",
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
    _check_form_token(request, review_csrf, "workhub:page-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        prior = conn.execute(
            """SELECT interface_id, status FROM workhub.interfaces
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
            """UPDATE workhub.interfaces SET status = 'pending', review_note = NULL, updated_at = now()
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
    _check_form_token(request, review_csrf, "workhub:page-form:v1")
    base = f"/departments/{org_unit_id}/{interface_id}"
    with connection() as conn:
        unit = _resolve_department(conn, principal, org_unit_id)
        if unit is None:
            return _denied(request, "你不在这个部门的范围内。")
        prior = conn.execute(
            """SELECT interface_id, status, reviewed_at FROM workhub.interfaces
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
            """UPDATE workhub.interfaces SET status = 'draft', updated_at = now()
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
            "SELECT descriptor, version_no FROM workhub.interfaces WHERE interface_id = %s",
            (row["based_on"],)).fetchone()
    if reference is None:
        reference = conn.execute(
            """SELECT descriptor, version_no FROM workhub.interfaces
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (row["org_unit_id"], row["slug"])).fetchone()
    old_descriptor = reference["descriptor"] if reference is not None else None
    bundle = review_rules.review_bundle(old_descriptor, row.get("descriptor"))
    bundle["reference_version"] = reference["version_no"] if reference is not None else None
    return bundle


@router.get("/reviews", response_class=HTMLResponse)
def review_queue(request: Request, saved: str | None = None, error: str | None = None):
    """The company side: pages a department finished and wants approved."""
    try:
        principal = _require(request, "workforce.interfaces", "review")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    except _NotPermitted:
        return _denied(request, "审核部门页面需要 workforce.interfaces/review 权限。")
    with connection() as conn:
        pending = conn.execute(
            """SELECT i.interface_id, i.name, i.slug, i.version_no, i.visibility, i.schema_version,
                      i.descriptor, i.based_on,
                      o.name AS org_name, o.org_unit_id, p.title AS position_title,
                      m.display_name AS author, i.updated_at
               FROM workhub.interfaces i
               JOIN workhub.org_units o USING (org_unit_id)
               LEFT JOIN workhub.positions p ON p.position_id = i.position_id
               LEFT JOIN workhub.workforce_members m ON m.member_id = i.updated_by
               WHERE i.status = 'pending'
               ORDER BY i.updated_at""").fetchall()
        decided = conn.execute(
            """SELECT i.interface_id, i.name, i.status, i.review_note, i.review_code, o.name AS org_name,
                      m.display_name AS reviewer, i.reviewed_at
               FROM workhub.interfaces i
               JOIN workhub.org_units o USING (org_unit_id)
               LEFT JOIN workhub.workforce_members m ON m.member_id = i.reviewed_by
               WHERE i.reviewed_at IS NOT NULL
               ORDER BY i.reviewed_at DESC LIMIT 10""").fetchall()
    # Every pending page is read as a diff against the version it was written
    # against, not as a whole page: that is the only part a human still has to
    # think about, and the part where a quiet re-mapping would otherwise slip by.
    with connection() as conn:
        review_cards = [dict(row, bundle=_review_bundle(conn, row)) for row in pending]
    # The decision list reads a code, not prose: "what do departments keep getting
    # wrong" has to be answerable by counting, not by re-reading old notes.
    decided_cards = [dict(row, code_label=review_rules.rejection_label(row["review_code"]))
                     for row in decided]
    return TEMPLATES.TemplateResponse(request, "reviews.html", {
        "pending": review_cards, "decided": decided_cards, "member": _member(request),
        "saved": saved, "error": error, "reasons": review_rules.REJECTION_CHOICES,
        "csrf": _form_token(request, "workhub:review-form:v1")})


@router.post("/reviews/{interface_id}/approve")
def review_approve(request: Request, interface_id: UUID, csrf: str = Form(...),
                   note: str = Form(default="")):
    """Approve a page: it becomes the live screen for that department slug."""
    try:
        principal = _require(request, "workforce.interfaces", "review")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    except _NotPermitted:
        return _denied(request, "审核部门页面需要 workforce.interfaces/review 权限。")
    _check_form_token(request, csrf, "workhub:review-form:v1")
    with connection() as conn:
        page = conn.execute(
            """SELECT interface_id, org_unit_id, slug, status, version_no, descriptor, based_on
               FROM workhub.interfaces WHERE interface_id = %s FOR UPDATE""",
            (interface_id,)).fetchone()
        if page is None:
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("找不到这个页面"), status_code=303)
        if page["status"] != "pending":
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("这个页面不在待审核状态"),
                                    status_code=303)
        # Approving replaces whatever was live for this slug: a department must
        # never end up with two pages claiming to be the current one.
        conn.execute(
            """UPDATE workhub.interfaces SET status = 'archived', updated_at = now()
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (page["org_unit_id"], page["slug"]))
        # The audience the department asked for only takes effect now: a public
        # template has to be signed off, so the row keeps 'department' while the
        # page waits and the approval is what promotes it.
        requested_visibility = review_rules.visibility_of(page["descriptor"] or {})
        conn.execute(
            """UPDATE workhub.interfaces
                  SET status = 'active', reviewed_by = %s, reviewed_at = now(),
                      review_note = %s, visibility = COALESCE(%s, visibility),
                      updated_at = now()
                WHERE interface_id = %s""",
            (principal["member_id"], note.strip() or None,
             requested_visibility if requested_visibility in ("department", "public") else None,
             interface_id))
        # The approval records what was actually reviewed, so "why did the number
        # move on the 12th" can be answered from the audit log alone.
        bundle = _review_bundle(conn, dict(page))
        summary = bundle["summary"]["headline"] if bundle else ""
        api.audit(conn, principal["member_id"], "interface.approved", "interface", interface_id,
                  (note.strip() or "review approved") + (f"｜{summary}" if summary else ""),
                  {"to": "active", "version_no": page["version_no"],
                   "changes": bundle["summary"]["total"] if bundle else 0})
    return RedirectResponse(url="/reviews?saved=approved", status_code=303)


@router.post("/reviews/{interface_id}/reject")
def review_reject(request: Request, interface_id: UUID, csrf: str = Form(...),
                  note: str = Form(...), reason_code: str = Form(default="")):
    """Send a page back with a reason; the department keeps its trial entries."""
    try:
        principal = _require(request, "workforce.interfaces", "review")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/reviews", status_code=302)
    except _NotPermitted:
        return _denied(request, "审核部门页面需要 workforce.interfaces/review 权限。")
    _check_form_token(request, csrf, "workhub:review-form:v1")
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
        page = conn.execute(
            "SELECT interface_id, status FROM workhub.interfaces WHERE interface_id = %s FOR UPDATE",
            (interface_id,)).fetchone()
        if page is None or page["status"] != "pending":
            return RedirectResponse(url="/reviews?error=" + urllib.parse.quote("这个页面不在待审核状态"),
                                    status_code=303)
        conn.execute(
            """UPDATE workhub.interfaces
                  SET status = 'draft', reviewed_by = %s, reviewed_at = now(),
                      review_note = %s, review_code = %s, updated_at = now()
                WHERE interface_id = %s""",
            (principal["member_id"], note.strip(), code, interface_id))
        api.audit(conn, principal["member_id"], "interface.rejected", "interface", interface_id,
                  note.strip(), {"to": "draft", "review_code": code})
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
                      (SELECT count(*) FROM workhub.interface_entries e
                        WHERE e.interface_id = i.interface_id AND e.status_entry = 'active') AS entry_count
               FROM workhub.interfaces i
               JOIN workhub.org_units o USING (org_unit_id)
               LEFT JOIN workhub.positions p ON p.position_id = i.position_id
               WHERE i.status = 'active' AND (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""", (company_scope, scoped)).fetchall()
    return TEMPLATES.TemplateResponse(request, "interfaces.html", {
        "interfaces": rows, "member": _member(request), "saved": saved, "error": error})



@router.get("/interfaces/board", response_class=HTMLResponse)
def interface_board(request: Request, stale_days: int = Query(default=7, ge=1, le=365)):
    """Manager view: what each department last reported through its own screen."""
    try:
        principal = _require(request, "workforce.reports", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看部门汇报汇总需要 workforce.reports/read 权限。")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        rows = conn.execute(
            """SELECT i.interface_id, i.name, i.slug, o.name AS org_name, p.title AS position_title,
                      e.title, e.status, e.blocked_reason, e.needs_decision, e.progress_value,
                      e.note, e.evidence, e.reported_at, m.display_name AS reporter,
                      e.extra
               FROM workhub.interfaces i
               JOIN workhub.org_units o USING (org_unit_id)
               LEFT JOIN workhub.positions p ON p.position_id = i.position_id
               LEFT JOIN LATERAL (
                   SELECT * FROM workhub.interface_entries
                    WHERE interface_id = i.interface_id AND status_entry = 'active'
                      AND entry_mode = 'live'
                    ORDER BY reported_at DESC, entry_id DESC LIMIT 1
               ) e ON true
               LEFT JOIN workhub.workforce_members m ON m.member_id = e.reported_by
               WHERE i.status = 'active' AND (%s OR i.org_unit_id = ANY(%s))
               ORDER BY o.name, i.name""", (company_scope, scoped)).fetchall()
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
            """SELECT i.*, o.name AS org_name FROM workhub.interfaces i
               JOIN workhub.org_units o USING (org_unit_id)
               WHERE i.interface_id = %s AND i.status = 'active'
                 AND (%s OR i.org_unit_id = ANY(%s))""", (interface_id, company_scope, scoped)).fetchone()
        if row is None:
            return _denied(request, "找不到这个部门界面，或你不在该部门范围内。")
        mode = _entry_mode_for(row["status"])
        latest = conn.execute(
            """SELECT * FROM workhub.interface_entries
               WHERE interface_id = %s AND status_entry = 'active' AND entry_mode = %s
               ORDER BY reported_at DESC, entry_id DESC LIMIT 1""", (interface_id, mode)).fetchone()
    descriptor = row["descriptor"]
    fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
    values = _interface_values(fields, latest)
    try:
        rendered = render_interface(descriptor, values=values)
    except RenderError as exc:
        return _denied(request, "这个界面描述未通过校验，暂不能填写：" + str(exc))
    return TEMPLATES.TemplateResponse(request, "interface_fill.html", {
        "interface": row, "rendered": rendered, "latest": latest,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "workhub:interface-form:v1")})


@router.post("/interfaces/{interface_id}")
async def interface_submit(request: Request, interface_id: UUID, csrf: str = Form(...)):
    try:
        principal = _require(request, "workforce.reports", "read")
        _require(request, "workforce.reports", "submit")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/interfaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "填写部门界面需要 workforce.reports/read 与 submit 权限。")
    _check_form_token(request, csrf, "workhub:interface-form:v1")
    with connection() as conn:
        company_scope = has_company_scope(conn, principal["member_id"])
        scoped = list(active_org_unit_ids(conn, principal["member_id"]))
        row = conn.execute(
            """SELECT * FROM workhub.interfaces WHERE interface_id = %s AND status = 'active'
                 AND (%s OR org_unit_id = ANY(%s))""", (interface_id, company_scope, scoped)).fetchone()
        if row is None:
            return _denied(request, "找不到这个部门界面，或你不在该部门范围内。")
        descriptor = row["descriptor"]
        fields = [f for f in descriptor.get("fields", []) if isinstance(f, dict) and f.get("key")]
        form = await request.form()
        standard, extra, problem = _read_entry_form(fields, descriptor.get("status_map", {}), form)
        if problem is not None:
            return RedirectResponse(url="/interfaces/" + str(interface_id) + "?error=" + urllib.parse.quote(problem),
                                    status_code=303)
        columns = dict(standard)
        columns["extra"] = Jsonb(extra)
        columns["reported_by"] = principal["member_id"]
        columns["entry_mode"] = _entry_mode_for(row["status"])
        keys = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))
        entry = conn.execute(
            "INSERT INTO workhub.interface_entries (interface_id, " + keys + ") VALUES (%s, " + placeholders + ")"
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
            """SELECT org_unit_id, org_code, name FROM workhub.org_units WHERE status = 'active'
                 AND (%s OR org_unit_id = ANY(%s)) ORDER BY org_code""", (company_scope, scoped)).fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.name AS org_name
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               WHERE p.status = 'active' AND (%s OR p.org_unit_id = ANY(%s))
               ORDER BY o.org_code, p.position_code""", (company_scope, scoped)).fetchall()
        existing = conn.execute(
            """SELECT i.interface_id, i.slug, i.name, i.status, i.visibility, i.version_no, o.name AS org_name
               FROM workhub.interfaces i JOIN workhub.org_units o USING (org_unit_id)
               WHERE (%s OR i.org_unit_id = ANY(%s)) ORDER BY o.name, i.name""",
            (company_scope, scoped)).fetchall()
    return TEMPLATES.TemplateResponse(request, "setup_interfaces.html", {
        "org_units": org_units, "positions": positions, "existing": existing,
        "member": _member(request), "saved": saved, "error": error,
        "csrf": _form_token(request, "workhub:interface-setup-form:v1")})

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
    _check_form_token(request, csrf, "workhub:interface-setup-form:v1")
    try:
        descriptor = json.loads(descriptor_text)
    except ValueError:
        return RedirectResponse(url="/setup/interfaces?error=json", status_code=303)
    if not isinstance(descriptor, dict):
        return RedirectResponse(url="/setup/interfaces?error=json", status_code=303)
    with connection() as conn:
        # The connection uses dict_row, so these come back keyed, not positional.
        codes = {row["org_code"] for row in conn.execute(
            "SELECT org_code FROM workhub.org_units WHERE status = 'active'").fetchall()}
        positions_known = {row["position_code"] for row in conn.execute(
            "SELECT position_code FROM workhub.positions WHERE status = 'active'").fetchall()}
        problems = validate_descriptor(descriptor, known_org_unit_codes=codes,
                                       known_position_codes=positions_known)
        if problems:
            return RedirectResponse(url="/setup/interfaces?error=" +
                                    urllib.parse.quote("；".join(problems)[:500]), status_code=303)
        require_org_unit_access(conn, principal["member_id"], org_unit_id)
        position = UUID(position_id) if position_id.strip() else None
        next_version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 AS next_version FROM workhub.interfaces
               WHERE org_unit_id = %s AND slug = %s""",
            (org_unit_id, slug.strip())).fetchone()["next_version"]
        # Redrawing a screen must not leave two of them claiming to be current:
        # the previous live version is archived in the same transaction.
        conn.execute(
            """UPDATE workhub.interfaces SET status = 'archived', updated_at = now()
               WHERE org_unit_id = %s AND slug = %s AND status = 'active'""",
            (org_unit_id, slug.strip()))
        row = conn.execute(
            """INSERT INTO workhub.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    position_id, version_no, created_by, updated_by)
               VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)
               RETURNING interface_id, version_no""",
            (org_unit_id, slug.strip(), name.strip(),
             (descriptor.get("meta") or {}).get("visibility", "department"),
             descriptor.get("schema_version", "0.1"), Jsonb(descriptor), position, next_version,
             principal["member_id"], principal["member_id"])).fetchone()
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
    with connection() as conn:
        if has_company_scope(conn, request.state.member["member_id"]):
            units = conn.execute(
                """SELECT DISTINCT o.org_unit_id, o.name FROM workhub.org_units o
                   JOIN workhub.work_items w USING (org_unit_id)
                   WHERE o.status = 'active' ORDER BY o.name""").fetchall()
        else:
            units = conn.execute(
                """SELECT DISTINCT o.org_unit_id, o.name FROM workhub.org_units o
                   JOIN workhub.work_items w USING (org_unit_id)
                   WHERE o.status = 'active' AND o.org_unit_id = ANY(%s)
                   ORDER BY o.name""", (list(active_org_unit_ids(conn, request.state.member["member_id"])),)).fetchall()
        departments = [{"unit": unit, "board": api.build_board(conn, unit["org_unit_id"], stale_days)}
                       for unit in units]
    totals = {key: sum(entry["board"]["summary"][key] for entry in departments)
              for key in ("open_items", "blocked", "needs_decision", "never_reported", "overdue")}
    return TEMPLATES.TemplateResponse(request, "progress.html", {
        "member": _member(request), "departments": departments, "totals": totals,
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
               FROM workhub.position_assignments a
               JOIN workhub.positions p USING (position_id)
               JOIN workhub.org_units o ON o.org_unit_id = p.org_unit_id
               WHERE a.member_id = %s AND a.status IN ('active', 'scheduled')
                 AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
                 AND p.status = 'active' ORDER BY o.name, p.title""",
            (principal["member_id"],)).fetchall()
        allowed = {row["position_id"] for row in assignments}
        items = conn.execute(
            """SELECT w.work_item_id, w.org_unit_id, w.position_id, w.title, w.status,
                      o.name AS org_name, p.title AS position_title, r.progress_value,
                      r.note, r.evidence, r.submitted_via, r.reported_at
               FROM workhub.work_items w JOIN workhub.positions p USING (position_id)
               JOIN workhub.org_units o ON o.org_unit_id = w.org_unit_id
               LEFT JOIN LATERAL (
                   SELECT progress_value, note, evidence, submitted_via, reported_at
                   FROM workhub.work_item_reports WHERE work_item_id = w.work_item_id
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
            """SELECT w.position_id, w.org_unit_id FROM workhub.work_items w
               WHERE w.work_item_id = %s""", (work_item_id,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="work item not found")
        owns_position = conn.execute(
            """SELECT 1 FROM workhub.position_assignments WHERE member_id = %s
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
                """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                   WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        else:
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                   WHERE status = 'active' AND org_unit_id = ANY(%s)
                   ORDER BY unit_type DESC, org_code""",
                (list(active_org_unit_ids(conn, request.state.member["member_id"])),)).fetchall()
        if not org_units:
            return TEMPLATES.TemplateResponse(request, "board.html",
                                              {"org_units": [], "board": None, "member": _member(request)})
        selected_id = org or org_units[0]["org_unit_id"]
        require_org_unit_access(conn, request.state.member["member_id"], selected_id)
        data = api.build_board(conn, selected_id, stale_days, layer, stage)
    # Computed after the connection closes: the data centre is a separate machine
    # on the LAN, and a slow one must not hold a database connection open.
    panel = company_metrics_panel()
    return TEMPLATES.TemplateResponse(request, "board.html", {
        "org_units": org_units,
        "selected_id": str(selected_id),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "stage": stage,
        "company_metrics": panel,
        "metrics_unavailable_prefix": METRICS_UNAVAILABLE_PREFIX,
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
                """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                   WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        else:
            org_units = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                   WHERE status = 'active' AND org_unit_id = ANY(%s)
                   ORDER BY unit_type DESC, org_code""",
                (list(active_org_unit_ids(conn, request.state.member["member_id"])),)).fetchall()
    # The htmx swap replaces the whole fragment, the metrics panel included, so
    # this renders the same panel the full page does -- not a stale copy.
    panel = company_metrics_panel()
    return TEMPLATES.TemplateResponse(request, "board_body.html", {
        "org_units": org_units,
        "selected_id": str(org),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "stage": stage,
        "company_metrics": panel,
        "metrics_unavailable_prefix": METRICS_UNAVAILABLE_PREFIX,
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


@router.get("/metrics/home.json")
def metrics_home_json(request: Request):
    """The company home metrics as data.

    Same numbers, same formatter and same wave as the board panel, so a dashboard
    or a script reads exactly what the page shows instead of recomputing it with a
    second rounding rule. Unavailable is a 200 with available=false: "the data
    centre is down" is a state of the world, not a client error, and a caller must
    be able to tell it apart from a refused permission.
    """
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return JSONResponse(status_code=401, content={
            "available": False, "reason": "需要登录", "wave": None, "metrics": []})
    except _NotPermitted:
        return JSONResponse(status_code=403, content={
            "available": False, "reason": "需要 workforce.work_items/read 权限", "wave": None, "metrics": []})
    return JSONResponse(content=company_metrics_panel())


@router.get("/office", response_class=HTMLResponse)
def office(request: Request, org: UUID | None = None, stale_days: int = Query(default=7, ge=1, le=365)):
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/office", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看办公室平面图需要 workforce.work_items/read 权限。")
    with connection() as conn:
        if not has_company_scope(conn, request.state.member["member_id"]):
            return _denied(request, "办公室总览目前仅对公司管理者开放。")
        floor = api.build_office(conn, stale_days)
    # With ?org= the room renders server-side on first paint, so the deep link and
    # the no-JavaScript path show the same thing the click does.
    unit = next((u for u in floor["units"] if str(u["org_unit_id"]) == str(org)), None) if org else None
    return TEMPLATES.TemplateResponse(request, "office.html", {
        "floor": floor, "org": str(org) if org else None, "unit": unit, "stale_days": stale_days,
        "member": _member(request), "status_labels": STATUS_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


@router.get("/office/zoom", response_class=HTMLResponse)
def office_zoom(request: Request, org: UUID, stale_days: int = Query(default=7, ge=1, le=365)):
    """One department blown up: its room, its desks, and its own flow strip. This
    is where the user's original idea lands -- click a department, see inside it."""
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/office", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看办公室平面图需要 workforce.work_items/read 权限。")
    with connection() as conn:
        floor = api.build_office(conn, stale_days)
    unit = next((unit for unit in floor["units"] if str(unit["org_unit_id"]) == str(org)), None)
    if unit is None:
        return HTMLResponse(
            "<p class=\"empty\">这个部门还没有画到平面图上。先到「布置」页给它一块区域。</p>",
            status_code=404)
    return TEMPLATES.TemplateResponse(request, "office_zoom.html", {
        "unit": unit, "stale_days": stale_days, "member": _member(request),
        "status_labels": STATUS_LABELS, "channel_labels": CHANNEL_LABELS,
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
            """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
               WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.name AS org_name
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               WHERE p.status = 'active' ORDER BY o.org_code, p.position_code""").fetchall()
        members = conn.execute(
            """SELECT member_id, employee_code, display_name, employment_status
               FROM workhub.workforce_members ORDER BY display_name""").fetchall()
    return TEMPLATES.TemplateResponse(request, "setup.html", {
        "org_units": org_units, "positions": positions, "members": members,
        "saved": saved, "error": error, "created_member_id": member_id,
        "login_token": login, "login_expires": expires, "member": _member(request),
        "csrf_org": _form_token(request, "workhub:org-form:v1"),
        "csrf_position": _form_token(request, "workhub:position-form:v1"),
        "csrf_member": _form_token(request, "workhub:member-form:v1"),
        "csrf_pilot": _form_token(request, "workhub:pilot-form:v1"),
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
    _check_form_token(request, csrf, "workhub:org-form:v1")
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
    _check_form_token(request, csrf, "workhub:position-form:v1")
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
    _check_form_token(request, csrf, "workhub:pilot-form:v1")
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


@router.get("/admin/spaces", response_class=HTMLResponse)
def admin_spaces(request: Request, org: UUID | None = None,
                 saved: str | None = None, error: str | None = None):
    try:
        principal = _require(request, "workforce.positions", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/spaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看办公室布置需要 workforce.positions/read 权限。")
    with connection() as conn:
        can_manage = has_permission(conn, principal["member_id"], "workforce.positions", "manage")
        org_units = conn.execute(
            """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.org_code, o.name AS org_name
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
              WHERE p.status = 'active' ORDER BY o.org_code, p.position_code""").fetchall()
        spaces = conn.execute(
            """SELECT s.space_id, s.kind, s.org_unit_id, s.position_id, s.label, s.x, s.y, s.w, s.h,
                      s.calibrated, o.name AS org_name, p.title AS position_title
                 FROM workhub.spaces s
                 JOIN workhub.org_units o USING (org_unit_id)
                 LEFT JOIN workhub.positions p ON p.position_id = s.position_id
                ORDER BY s.kind, o.org_code, s.position_id""").fetchall()
        # Two levels of the same canvas: the floor holds department zones, and ?org=
        # drops into one department where desks are laid out in coordinates local to
        # that room -- so moving a room never drags its furniture along.
        selected = None
        if org is not None:
            selected = conn.execute(
                """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
                    WHERE org_unit_id = %s AND status = 'active'""", (org,)).fetchone()
    placed = {row["org_unit_id"] for row in spaces if row["kind"] == "zone"}
    unplaced = [u for u in org_units if u["org_unit_id"] not in placed]
    return TEMPLATES.TemplateResponse(request, "admin_spaces.html", {
        "org_units": org_units, "positions": positions, "spaces": spaces,
        "selected": selected, "unplaced": unplaced,
        "can_manage": can_manage, "saved": saved, "error": error, "member": _member(request),
        "csrf": _form_token(request, "workhub:space-form:v1")})


@router.post("/admin/spaces")
def admin_save_space(request: Request,
                     csrf: str = Form(...),
                     kind: str = Form(...),
                     org_unit_id: UUID = Form(...),
                     position_id: str = Form(default=""),
                     label: str = Form(default=""),
                     x: int = Form(...), y: int = Form(...),
                     w: int = Form(...), h: int = Form(...),
                     calibrated: str = Form(default="")):
    try:
        principal = _require(request, "workforce.positions", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/spaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "修改办公室布置需要 workforce.positions/manage 权限。")
    _check_form_token(request, csrf, "workhub:space-form:v1")
    try:
        payload = SpaceUpsert(
            kind=kind, org_unit_id=org_unit_id,
            position_id=UUID(position_id) if position_id.strip() else None,
            label=label.strip() or None,
            x=x, y=y, w=w, h=h, calibrated=calibrated == "on")
    except (ValidationError, ValueError):
        return RedirectResponse(url="/admin/spaces?error=geometry", status_code=303)
    try:
        with connection() as conn:
            api.upsert_space(conn, payload, principal["member_id"])
    except HTTPException as exc:
        if exc.status_code == 409:
            return RedirectResponse(url="/admin/spaces?error=department", status_code=303)
        raise
    except CheckViolation:
        return RedirectResponse(url="/admin/spaces?error=geometry", status_code=303)
    return RedirectResponse(url="/admin/spaces?saved=1", status_code=303)


@router.post("/admin/spaces/{space_id}/remove")
def admin_remove_space(request: Request, space_id: UUID, csrf: str = Form(...)):
    try:
        principal = _require(request, "workforce.positions", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/spaces", status_code=302)
    except _NotPermitted:
        return _denied(request, "修改办公室布置需要 workforce.positions/manage 权限。")
    _check_form_token(request, csrf, "workhub:space-form:v1")
    with connection() as conn:
        api.remove_space(conn, space_id, principal["member_id"])
    return RedirectResponse(url="/admin/spaces?saved=1", status_code=303)


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
                  FROM workhub.work_items w
                  JOIN workhub.positions p USING (position_id)
                  JOIN workhub.org_units o ON o.org_unit_id = w.org_unit_id
                  {scope_sql}
                 ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                                        WHEN 'planned' THEN 2 ELSE 3 END, w.updated_at DESC""", scope_params).fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.org_code
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               WHERE p.status = 'active' AND (%s OR p.org_unit_id = ANY(%s))
               ORDER BY o.org_code, p.position_code""", (company_scope, scoped_orgs)).fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_work_items.html", {
        "items": items, "positions": positions, "can_manage": can_manage,
        "created": created, "error": error, "member": _member(request),
        "status_labels": STATUS_LABELS, "kind_labels": KIND_LABELS,
        "csrf": _form_token(request, "workhub:work-item-form:v1"),
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
    _check_form_token(request, csrf, "workhub:work-item-form:v1")
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
            position = conn.execute("SELECT org_unit_id FROM workhub.positions WHERE position_id = %s",
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
                      (SELECT count(*) FROM workhub.position_assignments a
                        WHERE a.position_id = p.position_id AND a.status IN ('active', 'scheduled')
                          AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)) AS holder_count
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               ORDER BY o.org_code, p.position_code""").fetchall()
        org_units = conn.execute(
            "SELECT org_unit_id, org_code, name FROM workhub.org_units WHERE status = 'active' ORDER BY org_code"
        ).fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_positions.html", {
        "positions": rows, "org_units": org_units, "can_manage": can_manage,
        "created": created, "error": error, "member": _member(request),
        "csrf": _form_token(request, "workhub:position-form:v1")})


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
    _check_form_token(request, csrf, "workhub:position-form:v1")
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
               FROM workhub.workforce_audit_events e
               LEFT JOIN workhub.workforce_members m ON m.member_id = e.actor_member_id
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
                         FROM workhub.position_assignments a
                         JOIN workhub.positions held ON held.position_id = a.position_id
                        WHERE held.org_unit_id = u.org_unit_id
                          AND a.status = 'active') AS member_count,
                      (SELECT count(*)
                         FROM workhub.interfaces i
                        WHERE i.org_unit_id = u.org_unit_id
                          AND i.status = 'active') AS page_count
               FROM workhub.org_units u
               LEFT JOIN workhub.org_units parent ON parent.org_unit_id = u.parent_org_unit_id
               WHERE u.status = 'active'
               ORDER BY u.name""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.title, p.position_code, o.name AS org_name
               FROM workhub.positions p
               JOIN workhub.org_units o ON o.org_unit_id = p.org_unit_id
               WHERE p.status = 'active'
               ORDER BY o.name, p.title""").fetchall()
        grants = conn.execute(
            """SELECT position_id, resource, action
               FROM workhub.permission_grants
               WHERE subject_type = 'position' AND revoked_at IS NULL
               ORDER BY resource, action""").fetchall()
        events = conn.execute(
            """SELECT e.event_type, e.subject_type, e.reason, e.occurred_at,
                      m.display_name AS actor_name
               FROM workhub.workforce_audit_events e
               LEFT JOIN workhub.workforce_members m ON m.member_id = e.actor_member_id
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
