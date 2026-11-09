"""接入方的票能碰到哪些路由：白名单，默认拒绝。

2026-11-09 实测出来的洞：一张只申请「读本部门条目」的票，把 `POST /v1/org-units`、
`POST /v1/members`、`POST /v1/grants` 全打通了——最后一个还一次发出公司级管理授权。
根因是范围检查只写在 `web.py` 的十二个出口上，`api.py` 三十二个端点一个都没写，
而「有票就能进」在那些端点上等于「有票就等于那个人」。

所以这里不逐个端点补参数，反过来验一张**白名单**：不在表里的路由，票一律 403。
本文件里最要紧的是那条**穷举闸门**——它遍历 `app.routes`，以后谁加了新端点又忘了
想清楚「要不要给票开」，闸门自己会红。逐端点补必然漏，穷举闸门不会。
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path
import test_connected_apps as edges  # noqa: E402  同一套真客户端与建票流程

pytestmark = pytest.mark.db

#: 白名单外被拦时那句中文——所有拒绝共用一个措辞，用例只认它的头半句。
REFUSAL = "这张票没有被允许做这件事"

#: 管理面里最要命的几条：都是「改组织」而不是「用组织」。逐条点名，别只靠穷举闸门，
#: 这样红的时候能直接看出是哪一扇门开了。
FORBIDDEN_CALLS = [
    ("POST", "/v1/org-units", {"org_code": "EVIL", "name": "私自开的部门"}),
    ("POST", "/v1/members", {"employee_code": "EVIL01", "display_name": "私自造的人"}),
    ("POST", "/v1/grants", {"subject_type": "member", "member_id": "x",
                            "resource": "workforce.org_units", "action": "manage",
                            "scope_type": "all"}),
    ("GET", "/v1/grants", None),
    ("GET", "/v1/members", None),
    ("GET", "/v1/org-units", None),
    ("GET", "/v1/audit-events", None),
    ("GET", "/v1/assignments", None),
]


@pytest.fixture(scope="module")
def world(db_ready):
    """一个自己的库：迁表 + 管理员 + 一个 GA 部门，别的都不靠。"""

    import psycopg

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            ["bootstrap-admin", "--name", "管理员", "--employee-code", "ADMIN001",
             "--org-code", "COMPANY", "--org-name", "公司"],
            url,
        )
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                   SELECT 'GA', '行政部', 'department', org_unit_id
                     FROM oams.org_units WHERE org_code = 'COMPANY'"""
            )
            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'"
            ).fetchone()[0]
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "admin_member_id": str(admin_id)}
    finally:
        suite._drop_database(db_ready, name)


def _read_only_ticket(world) -> str:
    """换一张只被允许「读本部门条目」的票——这就是那个能改成公司管理员的票。"""

    code = edges._issue_code(world, ["entries:read:dept"])
    assert edges._allow(world, code["code"], ["entries:read:dept"]) in (302, 303)
    status, ticket = edges._exchange(world, code["code"])
    assert status == 200, ticket
    return ticket["token"]


def _call(base: str, method: str, path: str, payload=None, token: str | None = None,
          cookies=None) -> tuple[int, object]:
    """任意方法的调用；4xx 也当结果返回。"""

    import json

    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if cookies is not None and len(cookies):
        request.add_header("Cookie", "; ".join(f"{c.name}={c.value}" for c in cookies))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, edges._decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, edges._decode(error.read())


# ---------------------------------------------------------------------------
# ① 那扇开过的门：只读票打管理面，每条都得是 403 且说人话
# ---------------------------------------------------------------------------


def test_a_read_only_ticket_cannot_reach_the_admin_endpoints(world):
    token = _read_only_ticket(world)
    for method, path, payload in FORBIDDEN_CALLS:
        status, body = _call(world["base"], method, path, payload, token)
        assert status == 403, (method, path, status, body)
        assert REFUSAL in body["detail"], (method, path, body)


def test_the_read_only_ticket_still_reaches_what_it_was_granted(world):
    """拦得太狠一样是 bug：白名单里那几扇门要真能进去，不能一律 403。

    `/v1/me` 是票自己的身份；`GET /v1/interfaces/{id}` 用一个不存在的 id 探——
    404 说明请求到了处理函数里，403 才会说明白名单把它也关了。
    """

    token = _read_only_ticket(world)
    status, body = _call(world["base"], "GET", "/v1/me", token=token)
    assert status == 200, body
    assert body["app"]["scopes"] == ["entries:read:dept"], body

    status, body = _call(
        world["base"], "GET", "/v1/interfaces/00000000-0000-0000-0000-000000000000",
        token=token)
    assert status == 404, body


# ---------------------------------------------------------------------------
# ② 穷举闸门：遍历真路由表，凡不在白名单里的，只读票一律 403
# ---------------------------------------------------------------------------


def _concrete_path(template: str) -> str:
    """把 `/v1/grants/{grant_id}/revoke` 变成一个真能发出去的地址。"""

    return re.sub(r"\{[^}]+\}", "11111111-1111-1111-1111-111111111111", template)


def _method_of(route) -> str:
    methods = {m.upper() for m in (getattr(route, "methods", None) or {"GET"})}
    methods.discard("HEAD")
    methods.discard("OPTIONS")
    return sorted(methods)[0] if methods else "GET"


def test_every_route_outside_the_whitelist_refuses_the_ticket(world):
    """这道闸门是为「以后」写的：新加一个端点、忘了想清楚要不要给票开，这里就红。

    判据不是「这条路由不在表里」——那只是同一份表自己跟自己比。判据是**真发一次**
    请求：不在表里的必须 403，在表里的放过不测（那些由别的用例负责）。
    """

    from oams import api as api_module

    token = _read_only_ticket(world)
    checked = 0
    for route in api_module._iter_routes(api_module.app.routes):
        template = getattr(route, "path", None)
        path_regex = getattr(route, "path_regex", None)
        methods = getattr(route, "methods", None)
        if not template or path_regex is None or not methods:
            continue  # Mount（/static）这类没有方法的跳过去
        path = _concrete_path(template)
        # 星移的匹配顺序就是注册顺序：真被哪条路由接走，比的就是哪条
        effective = api_module._matched_route(path)
        assert effective is not None, f"{path} 没被任何路由认领，闸门测不到它"
        if api_module.app_route_allowed(_method_of(route), effective):
            continue
        status, body = _call(world["base"], _method_of(route), path, token=token)
        assert status == 403, (effective, path, status, body)
        assert REFUSAL in body["detail"], (effective, path, body)
        checked += 1
    assert checked >= 20, f"只测到 {checked} 条，路由表是不是没加载全"


def test_the_whitelist_itself_names_no_admin_endpoint(world):
    """表里那条「管理面一条都没进」，单独钉一次：表被改宽时立刻看得见。"""

    from oams import api as api_module

    for method, template in api_module.APP_ROUTE_WHITELIST:
        assert not template.startswith("/v1/grants"), template
        assert not template.startswith("/v1/org-units"), template
        assert not template.startswith("/v1/members"), template
        assert not template.startswith("/v1/positions"), template
        assert not template.startswith("/v1/assignments"), template
        assert not template.startswith("/v1/audit-events"), template
        assert not template.startswith("/v1/board"), template
        # 工作项只开了「读列表 / 读进展 / 写进展」三扇门，**建工作项那一步必须关着**：
        # 工作项是组织对「该做什么」的判断，给票开了，看板就会长出 AI 自己想象的任务。
        assert (method.upper(), template) != ("POST", "/v1/work-items"), template
        assert not template.startswith("/v1/work-items/{work_item_id}/retract"), template


# ---------------------------------------------------------------------------
# ③ 票不能当浏览器会话：cookie 里塞票打工作台，一样 403
# ---------------------------------------------------------------------------


def test_a_ticket_in_a_cookie_cannot_open_the_workbench(world):
    """表单令牌是拿 cookie 值当密钥算的——票要是能被当会话，它就能自己签发它。"""

    from http.cookiejar import CookieJar

    from oams.config import settings

    token = _read_only_ticket(world)
    jar = CookieJar()
    # 手工造一个 cookie：这条请求不带 Authorization，只看 cookie
    import urllib.request as _request

    class _OneCookie(_request.Request):
        pass

    request = _request.Request(world["base"] + "/board")
    request.add_header("Cookie", f"{settings.web_session_cookie}={token}")
    opener = _request.build_opener(_request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode()
    assert status == 403, text
    assert "接入方的票不能当浏览器会话用" in text, text
    assert jar is not None  # 只是让 CookieJar 的导入不被当成没用


# ---------------------------------------------------------------------------
# ③b 票只能断开它自己那条：白名单放行了「断开自己」，不等于放行「断开别人」
# ---------------------------------------------------------------------------


def test_a_ticket_can_only_revoke_its_own_authorization(world):
    """只读票代表的人正好是公司管理员时，它能断掉谁的授权？

    白名单把 `/api/apps/authorizations/{id}/revoke` 放给票了（票得能断开自己），
    而共用实现 `revoke_authorization` 原本只判「本人或公司管理员」。于是这张只申请了
    「读本部门条目」的票，凭它代表的那位管理员的身份，能把公司里任何一个人的授权断掉
    ——票据范围在这一条上等于不存在（2026-11-09 实测）。
    """

    import psycopg

    token = _read_only_ticket(world)

    with psycopg.connect(world["url"], autocommit=True) as conn:
        other = conn.execute(
            """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
               VALUES ('PROBE02', '旁观者', 'active') RETURNING member_id""").fetchone()[0]
        app_id = conn.execute("SELECT app_id FROM oams.connected_apps LIMIT 1").fetchone()[0]
        foreign = conn.execute(
            """INSERT INTO oams.app_authorizations (app_id, member_id, scopes, valid_until, approved_by)
               VALUES (%s, %s, ARRAY['entries:read:dept'], now() + interval '7 days', %s)
               RETURNING authorization_id""", (app_id, other, other)).fetchone()[0]

    status, body = _call(world["base"], "POST",
                         f"/api/apps/authorizations/{foreign}/revoke",
                         {"reason": "票来断别人的"}, token)
    assert status == 403, body
    assert "只能断开它自己那条授权" in body["detail"], body

    with psycopg.connect(world["url"], autocommit=True) as conn:
        untouched = conn.execute(
            "SELECT revoked_at FROM oams.app_authorizations WHERE authorization_id = %s",
            (foreign,)).fetchone()
    assert untouched[0] is None, "别人的授权被票断掉了"

    # 同一张票断自己那条仍是通的——收窄不能收到「票连自己都断不了」。
    status, body = _call(world["base"], "GET", "/v1/me", token=token)
    assert status == 200, body
    own = body["app"]["authorization_id"] if "authorization_id" in body.get("app", {}) else None
    if own is None:
        _, listed = _call(world["base"], "GET", "/api/apps/authorizations", token=token)
        own = listed[0]["authorization_id"]
    status, body = _call(world["base"], "POST",
                         f"/api/apps/authorizations/{own}/revoke",
                         {"reason": "断开自己"}, token)
    assert status == 200, body
    assert body["revoked"] is True, body


# ---------------------------------------------------------------------------
# ④ 人自己不受影响：普通会话票照旧能打管理面
# ---------------------------------------------------------------------------


def test_a_person_session_is_not_blocked_by_the_app_whitelist(world):
    """白名单只收「票」，不是收「所有带 Bearer 的请求」——收错就是把人锁在门外。"""

    from oams.security import issue_session

    token, _ = issue_session(world["admin_member_id"])
    status, body = _call(world["base"], "GET", "/v1/grants", token=token)
    assert status == 200, body
    status, body = _call(world["base"], "GET", "/v1/org-units", token=token)
    assert status == 200, body
