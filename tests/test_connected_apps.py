"""接入方（connected app）的安全边界。

这套东西**不能**做什么，比它能做什么更重要：一张票只代表某个人点过一次允许，
范围外的动作一律 403，撤销的那一刻手里那张票立刻作废。所以这里不测「能建出页面」，
测的是四道门：没批准不给票、一个码只能换一次、范围外不给做、撤销后旧票立刻 401。

全部走真库（每次建一个自己的库：迁表 + bootstrap-admin + 一个 GA 部门）与真路由，
不 mock `issue_session`；「人」那一半用真 cookie 走 `/login` 与 `/connect/{code}`。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[1]
GA_DESCRIPTOR = json.loads(
    (REPO / "data" / "neutral-demo-ga-parcel-lookup.json").read_text(encoding="utf-8")
)
SCOPES_ALL = ["interfaces:draft", "entries:read:dept", "entries:write"]


# ---------------------------------------------------------------------------
# 一套自己的世界：库、管理员、GA 部门，都只属于这个文件
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(db_ready):
    """一个独立的库：迁表 + 管理员 + GA 部门；页面本身由用例现建。"""

    import psycopg

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            [
                "bootstrap-admin",
                "--name", "管理员",
                "--employee-code", "ADMIN001",
                "--org-code", "COMPANY",
                "--org-name", "公司",
            ],
            url,
        )
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                   SELECT 'GA', '行政部', 'department', org_unit_id
                     FROM oams.org_units WHERE org_code = 'COMPANY'"""
            )
            conn.execute(
                """INSERT INTO oams.positions (position_code, title, org_unit_id)
                   SELECT 'GA_LEAD', '行政负责人', org_unit_id
                     FROM oams.org_units WHERE org_code = 'GA'"""
            )
            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'"
            ).fetchone()[0]
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "admin_member_id": str(admin_id)}
    finally:
        suite._drop_database(db_ready, name)


def _sql(world, sql: str, args=()):
    """改世界里的行（例如把码改到过去，不必等 10 分钟）。"""

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(world["url"], row_factory=dict_row) as conn:
        cursor = conn.execute(sql, args)
        rows = cursor.fetchall() if cursor.description is not None else []
        conn.commit()
    return rows


# ---------------------------------------------------------------------------
# 两个客户端：一个是应用（Bearer），一个是人（cookie、不跟重定向）
# ---------------------------------------------------------------------------


def _decode(raw: bytes):
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _post_json(base: str, path: str, payload: dict | None = None, token: str | None = None,
               cookies: CookieJar | None = None) -> tuple[int, object]:
    """JSON 出口的调用；4xx 也当结果返回，不抛。"""

    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method="POST")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if cookies is not None and len(cookies):
        request.add_header("Cookie", "; ".join(f"{c.name}={c.value}" for c in cookies))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode(error.read())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # 302/303 要能被断言，不自动跟
        return None


class Human:
    """人那一半：带 cookie、不跟重定向的极简浏览器。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect,
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def get(self, path: str) -> tuple[int, str]:
        request = urllib.request.Request(self.base + path)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def post(self, path: str, form: dict) -> int:
        payload = urllib.parse.urlencode(form, doseq=True).encode()
        request = urllib.request.Request(self.base + path, data=payload, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code


def _signed_in(world) -> Human:
    """以管理员身份真登录一次，拿到会话 cookie。"""

    from oams.security import issue_session

    human = Human(world["base"])
    token, _ = issue_session(world["admin_member_id"])
    status = human.post("/login", {"token": token})
    assert status in (200, 302, 303), status
    return human


def _issue_code(world, scopes: list[str], name: str = "外部应用") -> dict:
    status, body = _post_json(world["base"], "/api/apps/codes", {"name": name, "scopes": scopes})
    assert status == 201, body
    return body


def _allow(world, code: str, scopes: list[str], days: int = 7) -> int:
    """人打开那一屏、只勾要勾的范围、点允许；返回端点的状态码。"""

    human = _signed_in(world)
    status, html = human.get(f"/connect/{code}")
    assert status == 200, status
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "允许那一屏没有表单令牌"
    return human.post(
        f"/connect/{code}", {"csrf": match.group(1), "scope": scopes, "days": str(days)}
    )


def _exchange(world, code: str) -> tuple[int, dict]:
    return _post_json(world["base"], "/api/apps/token", {"code": code})


def _create_page(world, slug: str) -> dict:
    """用一张全范围的票真建一页草稿，后面几组用例拿它当靶子。"""

    ticket_code = _issue_code(world, SCOPES_ALL)
    assert _allow(world, ticket_code["code"], SCOPES_ALL) in (302, 303)
    status, ticket = _exchange(world, ticket_code["code"])
    assert status == 200, ticket
    status, page = _post_json(
        world["base"], "/v1/interfaces",
        {"slug": slug, "name": "接入方靶页", "org_unit_code": "GA", "descriptor": GA_DESCRIPTOR},
        token=ticket["token"],
    )
    assert status == 201, page
    return page


def _entry_values() -> dict:
    values = {"title": "接入方条目", "evidence": "APP-EDGE-01"}
    mapping = GA_DESCRIPTOR.get("status_map") or {}
    for field in GA_DESCRIPTOR.get("fields", []):
        if field.get("map_to") == "status" and mapping:
            values[field["key"]] = next(iter(mapping))
    return values


# ---------------------------------------------------------------------------
# ① 人没点允许之前，码换不出票
# ---------------------------------------------------------------------------


def test_a_code_hands_out_no_ticket_before_a_human_approves(world):
    code = _issue_code(world, ["interfaces:draft"])
    status, body = _exchange(world, code["code"])
    assert status == 409
    assert "还没有人点允许" in body["detail"]


def test_an_unknown_code_is_a_404_not_a_ticket(world):
    status, body = _exchange(world, "000000")
    assert status == 404
    assert "没有这个码" in body["detail"]


# ---------------------------------------------------------------------------
# ② 一个码只换一次
# ---------------------------------------------------------------------------


def test_one_code_exchanges_exactly_once(world):
    code = _issue_code(world, ["interfaces:draft"])
    assert _allow(world, code["code"], ["interfaces:draft"]) in (302, 303)

    first, ticket = _exchange(world, code["code"])
    assert first == 200, ticket
    assert ticket["token"]

    second, refusal = _exchange(world, code["code"])
    assert second == 409
    assert "已经换过票" in refusal["detail"]


# ---------------------------------------------------------------------------
# ③ 码过期：改库里那一行，别等 10 分钟
# ---------------------------------------------------------------------------


def test_an_expired_code_says_so_on_the_page_and_at_the_exchange(world):
    code = _issue_code(world, ["interfaces:draft"])
    # 约束要求「过期时间晚于创建时间」：两个都往前挪，码就真的过期了，不用等十分钟
    _sql(
        world,
        """UPDATE oams.app_codes
              SET created_at = now() - interval '20 minutes',
                  expires_at = now() - interval '10 minutes'
            WHERE code = %s""",
        (code["code"],),
    )

    human = _signed_in(world)
    status, html = human.get("/connect/" + code["code"])
    assert status == 200
    assert "过期" in html
    assert 'name="csrf"' not in html, "过期的码不该还给出「允许」表单"

    status, body = _exchange(world, code["code"])
    assert status == 410
    assert "过期" in body["detail"]


# ---------------------------------------------------------------------------
# ④ 范围：票只做被勾的那几件事
# ---------------------------------------------------------------------------


def test_a_read_only_ticket_cannot_write_and_names_the_missing_permission(world):
    page = _create_page(world, "edge-read-" + uuid.uuid4().hex[:6])

    code = _issue_code(world, SCOPES_ALL)
    # 人只勾了「读」——应用申请了写，也没用
    assert _allow(world, code["code"], ["entries:read:dept"]) in (302, 303)
    status, ticket = _exchange(world, code["code"])
    assert status == 200, ticket

    status, body = _post_json(
        world["base"], f"/v1/interfaces/{page['interface_id']}/entries",
        {"values": _entry_values()}, token=ticket["token"],
    )
    assert status == 403, body
    assert "没有被允许" in body["detail"] and "填报条目" in body["detail"], body


def test_a_write_only_ticket_cannot_read_entries(world):
    page = _create_page(world, "edge-write-" + uuid.uuid4().hex[:6])

    code = _issue_code(world, ["entries:read:dept", "entries:write"])
    assert _allow(world, code["code"], ["entries:write"]) in (302, 303)
    status, ticket = _exchange(world, code["code"])
    assert status == 200, ticket

    # GET 不是 JSON 出口，用一条普通请求
    request = urllib.request.Request(
        world["base"] + f"/v1/interfaces/{page['interface_id']}/entries",
        headers={"Authorization": "Bearer " + ticket["token"]},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode()
    assert status == 403, text
    assert "读本部门的填报条目" in text


# ---------------------------------------------------------------------------
# ⑤ 撤销的那一刻，手里的票立刻作废
# ---------------------------------------------------------------------------


def test_revoking_an_authorization_kills_the_ticket_already_in_hand(world):
    page = _create_page(world, "edge-revoke-" + uuid.uuid4().hex[:6])
    code = _issue_code(world, SCOPES_ALL)
    assert _allow(world, code["code"], SCOPES_ALL) in (302, 303)
    status, ticket = _exchange(world, code["code"])
    assert status == 200, ticket

    def read_entries(token: str) -> tuple[int, str | dict]:
        request = urllib.request.Request(
            world["base"] + f"/v1/interfaces/{page['interface_id']}/entries",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    before, _ = read_entries(ticket["token"])
    assert before == 200

    human = _signed_in(world)
    status, revoked = _post_json(
        world["base"], f"/api/apps/authorizations/{ticket['authorization_id']}/revoke",
        {"reason": "用例收尾"}, cookies=human.jar,
    )
    assert status == 200, revoked
    assert revoked["revoked"] is True
    assert revoked["sessions_revoked"] >= 1, revoked

    after, body = read_entries(ticket["token"])
    assert after == 401, body

    sessions = _sql(
        world,
        """SELECT session_id, revoked_at FROM oams.workforce_sessions
            WHERE member_id = %s AND revoked_at IS NOT NULL""",
        (world["admin_member_id"],),
    )
    assert sessions, "撤销之后库里应该留得下「票被吊销」的痕迹"


# ---------------------------------------------------------------------------
# ⑥ 审计：谁替谁允许、允许了什么，事后查得到
# ---------------------------------------------------------------------------


def test_every_approval_and_revoke_leaves_an_audit_trail_naming_the_app(world):
    code = _issue_code(world, ["entries:read:dept"], name="审计用应用")
    assert _allow(world, code["code"], ["entries:read:dept"]) in (302, 303)
    status, ticket = _exchange(world, code["code"])
    assert status == 200, ticket

    human = _signed_in(world)
    status, _ = _post_json(
        world["base"], f"/api/apps/authorizations/{ticket['authorization_id']}/revoke",
        {}, cookies=human.jar,
    )
    assert status == 200

    rows = _sql(
        world,
        """SELECT event_type, reason, metadata->>'app_id' AS app_id
             FROM oams.workforce_audit_events
            WHERE subject_type = 'app_authorization' AND metadata->>'app_id' = %s
            ORDER BY occurred_at""",
        (code["app_id"],),
    )
    kinds = [row["event_type"] for row in rows]
    assert "app.authorization.approved" in kinds, kinds
    assert "app.authorization.revoked" in kinds, kinds
    assert all("审计用应用" in row["reason"] for row in rows), rows


# ---------------------------------------------------------------------------
# ⑦ 人自己撤得掉：「我的授权」那页
# ---------------------------------------------------------------------------


def test_the_person_can_disconnect_from_my_authorizations(world):
    """允许过一次之后，页面里列得出来、点「断开」之后票就没了——不用找管理员。"""

    code = _issue_code(world, ["interfaces:draft", "entries:read:dept"], name="断开用应用")
    assert _allow(world, code["code"], ["interfaces:draft", "entries:read:dept"]) in (302, 303)
    status, ticket = _exchange(world, code["code"])
    assert status == 200, ticket

    human = _signed_in(world)
    status, html = human.get("/apps")
    assert status == 200, status
    assert "断开用应用" in html
    assert "新建草稿页面并送审" in html, "范围要用人话写出来，不能只给一个 entries:read:dept"

    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "「我的授权」页没有表单令牌"
    status = human.post(f"/apps/{ticket['authorization_id']}/revoke", {"csrf": match.group(1)})
    assert status == 303, status

    status, html = human.get("/apps")
    assert status == 200
    assert "断开用应用" not in html, "断开之后就不该再列出来"

    request = urllib.request.Request(
        world["base"] + "/api/apps/authorizations",
        headers={"Authorization": "Bearer " + ticket["token"]},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20):
            raise AssertionError("断开之后那张票不该还能用")
    except urllib.error.HTTPError as error:
        assert error.code == 401


# ---------------------------------------------------------------------------
# ⑧ 审计里那几行是中文，而且写清了是哪个应用动的手
# ---------------------------------------------------------------------------


def test_what_an_app_did_is_written_in_chinese_and_names_it(world):
    page = _create_page(world, "edge-audit-" + uuid.uuid4().hex[:6])

    rows = _sql(
        world,
        """SELECT reason, metadata->>'app_id' AS app_id
             FROM oams.workforce_audit_events
            WHERE event_type = 'interface.created' AND subject_id = %s""",
        (page["interface_id"],),
    )
    assert rows, "建页面这件事必须留下审计"
    assert "新建了页面草稿" in rows[0]["reason"], rows[0]["reason"]
    assert "「接入方靶页」" in rows[0]["reason"], rows[0]["reason"]
    assert "connected-app API" not in rows[0]["reason"], "审计理由不许是英文"
    assert rows[0]["app_id"], "按应用筛要靠 metadata.app_id，不能只靠名字"
