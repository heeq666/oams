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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from pydantic import ValidationError

from workhub import api
from workhub.config import settings
from workhub.db import connection
from workhub.schemas import PositionCreate, WorkItemCreate
from workhub.security import has_permission, principal_from_request, principal_for_token, revoke_session

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
# The page filters cards with the very predicate the API counts them with, so a
# lane can never show a card that the layer badge says is not there.
TEMPLATES.env.globals["in_layer"] = api.layer_matches

STATUS_LABELS = {"planned": "未开始", "in_progress": "进行中", "blocked": "阻塞",
                 "done": "已完成", "cancelled": "已取消"}
KIND_LABELS = {"project": "项目", "routine": "常规", "adhoc": "临时"}
CHANNEL_LABELS = {"human": "人工", "agent": "AI 代笔"}


def _safe_layer(value: str | None) -> str:
    """Unknown layer ids fall back to 'all' rather than rendering an empty board."""
    known = {layer_id for layer_id, _, _ in api.BOARD_LAYERS}
    return value if value in known else "all"


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
    return _login(request, next_url=_safe_next(next))


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
    if principal is not None:
        with connection() as conn:
            revoke_session(conn, principal["session_id"], principal["member_id"])
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(settings.web_session_cookie, path="/")
    return response


@router.get("/board", response_class=HTMLResponse)
def board(request: Request, org: UUID | None = None, stale_days: int = Query(default=7, ge=1, le=365),
          layer: str = Query(default="all")):
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看看板需要 workforce.work_items/read 权限。")
    layer = _safe_layer(layer)

    with connection() as conn:
        # ROW-LEVEL FILTERING GAP: this lists every org unit, exactly as the API
        # does today. When range filtering lands in security.effective_grants,
        # this has to narrow with it -- otherwise the UI stays wider than the API.
        org_units = conn.execute(
            """SELECT org_unit_id, org_code, name, unit_type FROM workhub.org_units
               WHERE status = 'active' ORDER BY unit_type DESC, org_code""").fetchall()
        if not org_units:
            return TEMPLATES.TemplateResponse(request, "board.html",
                                              {"org_units": [], "board": None, "member": _member(request)})
        selected_id = org or org_units[0]["org_unit_id"]
        data = api.build_board(conn, selected_id, stale_days)
    return TEMPLATES.TemplateResponse(request, "board.html", {
        "org_units": org_units,
        "selected_id": str(selected_id),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "member": _member(request),
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


@router.get("/board/body", response_class=HTMLResponse)
def board_body(request: Request, org: UUID, stale_days: int = Query(default=7, ge=1, le=365),
               layer: str = Query(default="all")):
    """The board content alone, so switching department or layer does not reload the page."""
    try:
        _require(request, "workforce.work_items", "read")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/board", status_code=302)
    except _NotPermitted:
        return _denied(request, "查看看板需要 workforce.work_items/read 权限。")
    layer = _safe_layer(layer)
    with connection() as conn:
        data = api.build_board(conn, org, stale_days)
    return TEMPLATES.TemplateResponse(request, "board_body.html", {
        "selected_id": str(org),
        "board": data,
        "stale_days": stale_days,
        "layer": layer,
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "channel_labels": CHANNEL_LABELS,
    })


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
        items = conn.execute(
            f"""SELECT {api.WORK_ITEM_SELECT}
                  FROM workhub.work_items w
                  JOIN workhub.positions p USING (position_id)
                  JOIN workhub.org_units o ON o.org_unit_id = w.org_unit_id
                 ORDER BY CASE w.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1
                                        WHEN 'planned' THEN 2 ELSE 3 END, w.updated_at DESC""").fetchall()
        positions = conn.execute(
            """SELECT p.position_id, p.position_code, p.title, o.org_code
               FROM workhub.positions p JOIN workhub.org_units o USING (org_unit_id)
               WHERE p.status = 'active' ORDER BY o.org_code, p.position_code""").fetchall()
    return TEMPLATES.TemplateResponse(request, "admin_work_items.html", {
        "items": items, "positions": positions, "can_manage": can_manage,
        "created": created, "error": error, "member": _member(request),
        "status_labels": STATUS_LABELS, "kind_labels": KIND_LABELS,
    })


@router.post("/admin/work-items")
def admin_create_work_item(request: Request,
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
        "created": created, "error": error, "member": _member(request)})


@router.post("/admin/positions")
def admin_create_position(request: Request,
                          position_code: str = Form(...),
                          title: str = Form(...),
                          org_unit_id: UUID = Form(...)):
    try:
        principal = _require(request, "workforce.positions", "manage")
    except _NotSignedIn:
        return RedirectResponse(url="/login?next=/admin/positions", status_code=302)
    except _NotPermitted:
        return _denied(request, "新建岗位需要 workforce.positions/manage 权限。")
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
