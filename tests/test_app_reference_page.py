"""「照着别的部门那一页做一版」：参考权在 AI 这条路上必须真的能用。

参考权（`can_base`）的意思一直是「给你页面定义，不给条目内容」。但对外 AI 拿不到
定义——`GET /v1/interfaces/{id}` 只认「能打开的页」，返回里也没有定义本身。于是
一个部门就算被开了参考权，AI 也没有起点，只能从零手写一份描述；而这个系统里写
描述的人本来就是 AI。

这个文件钉三件事：①拿到参考权的票能读到定义，且只看得到定义（条目一律 403）；
②没开给它的页照旧 403；③MCP 那条工具把定义交到 AI 手上，而不是让它在网页上点。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402
import test_connected_apps as edges  # noqa: E402  同一套真客户端与建票流程

pytestmark = pytest.mark.db

#: 建页 + 读条目这两档范围：照着一页做本部门一版时，票上必须有这两样。
BUILD_SCOPES = ["interfaces:draft", "entries:read:dept"]

SHARED_SLUG = "ref-parcel-lookup"
PRIVATE_SLUG = "ref-expense-ledger"
#: 有一行授权的记录、但三格全假：既不能看也不能参考，不该出现在「别人开给你的页」里。
BLIND_SLUG = "ref-blind-lookup"


def _descriptor(interface_id: str, *, name: str, slug: str, org: str, positions: list[str]) -> dict:
    return {
        "schema_version": "0.2",
        "interface_id": interface_id,
        "name": name,
        "org_unit_code": org,
        "position_codes": positions,
        "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
        "modules": [{"key": "layout", "type": "rows", "params": {"gap": "md"}, "children": []}],
        "fields": [
            {"key": "title", "label": "名称", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "顺丰 1 件"},
            {"key": "status", "label": "状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "待取",
             "options": ["待取", "已取"]},
        ],
        "status_map": {"待取": "planned", "已取": "done"},
        "rules": {},
        "meta": {"author_position_code": positions[0], "version": 1,
                 "visibility": "department", "review": {"status": "active"}},
        "slug": slug,
    }


@pytest.fixture(scope="module")
def world(db_ready):
    """一个自己的库：行政部与财务部各一个人；财务部两页，一页开参考权给行政。"""

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
            for code, label in (("GA", "行政部"), ("FIN", "财务部")):
                conn.execute(
                    """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                       SELECT %s, %s, 'department', org_unit_id
                         FROM oams.org_units WHERE org_code = 'COMPANY'""",
                    (code, label))
            for code, title, org in (("GA_LEAD", "行政负责人", "GA"),
                                     ("FIN_LEAD", "财务负责人", "FIN")):
                conn.execute(
                    """INSERT INTO oams.positions (position_code, title, org_unit_id)
                       SELECT %s, %s, org_unit_id FROM oams.org_units WHERE org_code = %s""",
                    (code, title, org))
            conn.execute(
                """INSERT INTO oams.permission_grants
                       (subject_type, position_id, resource, action, scope_type, valid_from, reason)
                   SELECT 'position', p.position_id, g.resource, g.action, 'all', now(), 'seed'
                     FROM oams.positions p
                     JOIN (VALUES ('workforce.reports', 'read'),
                                  ('workforce.reports', 'submit')) AS g(resource, action) ON true
                    WHERE p.position_code IN ('GA_LEAD', 'FIN_LEAD')"""
            )
            conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status, joined_at)
                   VALUES ('GA001', '行政小兵', 'active', current_date - 30),
                          ('FIN001', '财务小兵', 'active', current_date - 30)"""
            )
            conn.execute(
                """INSERT INTO oams.position_assignments
                       (member_id, position_id, assignment_type, status, starts_at, reason)
                   SELECT m.member_id, p.position_id, 'primary', 'active', now() - interval '30 days', 'seed'
                     FROM oams.workforce_members m JOIN oams.positions p ON true
                    WHERE (m.employee_code = 'GA001' AND p.position_code = 'GA_LEAD')
                       OR (m.employee_code = 'FIN001' AND p.position_code = 'FIN_LEAD')"""
            )
            fin_org = conn.execute(
                "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'FIN'").fetchone()[0]
            ga_org = conn.execute(
                "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'GA'").fetchone()[0]
            fin_pos = conn.execute(
                "SELECT position_id FROM oams.positions WHERE position_code = 'FIN_LEAD'"
            ).fetchone()[0]
            pages = {}
            for slug, display, org in ((SHARED_SLUG, "快递代收台账", "FIN"),
                                       (PRIVATE_SLUG, "报销明细台账", "FIN"),
                                       (BLIND_SLUG, "对不上的台账", "FIN")):
                page_id = str(__import__("uuid").uuid4())
                descriptor = _descriptor(page_id, name=display, slug=slug,
                                         org=org, positions=["FIN_LEAD"])
                conn.execute(
                    """INSERT INTO oams.interfaces
                           (interface_id, org_unit_id, slug, name, status, visibility,
                            schema_version, descriptor, position_id, version_no)
                       VALUES (%s, %s, %s, %s, 'active', 'department', '0.2', %s, %s, 1)""",
                    (page_id, fin_org, slug, display,
                     psycopg.types.json.Jsonb(descriptor), fin_pos))
                pages[slug] = page_id
            # 只给「可作参考」：能看定义，打不开、更读不到条目。
            conn.execute(
                """INSERT INTO oams.interface_access
                       (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base, granted_at)
                   VALUES (%s, %s, %s, false, false, true, now())""",
                (fin_org, SHARED_SLUG, ga_org))
            # 一行三格全假的授权：表格里躺着，但什么都没给。
            conn.execute(
                """INSERT INTO oams.interface_access
                       (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base, granted_at)
                   VALUES (%s, %s, %s, false, false, false, now())""",
                (fin_org, BLIND_SLUG, ga_org))
            members = {code: str(member_id) for code, member_id in conn.execute(
                "SELECT employee_code, member_id FROM oams.workforce_members").fetchall()}
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "members": members, "pages": pages}
    finally:
        suite._drop_database(db_ready, name)


def _ticket(world, member_code: str, scopes: list[str]) -> str:
    code = edges._issue_code(world, scopes)
    human = edges._signed_in_as(world, world["members"][member_code])
    status, html = human.get(f"/connect/{code['code']}")
    assert status == 200, status
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "允许那一屏没有表单令牌"
    assert human.post(f"/connect/{code['code']}",
                      {"csrf": match.group(1), "scope": scopes, "days": "7"}) in (302, 303)
    status, ticket = edges._exchange(world, code["code"])
    assert status == 200, ticket
    return ticket["token"]


def _call(base: str, method: str, path: str, payload=None, token: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, edges._decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, edges._decode(error.read())


# ---------------------------------------------------------------------------
# ① 参考权拿到的是定义，而且只有定义
# ---------------------------------------------------------------------------


def test_a_ticket_with_reference_right_reads_the_definition(world):
    token = _ticket(world, "GA001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET",
                         f"/v1/interfaces/{world['pages'][SHARED_SLUG]}", token=token)
    assert status == 200, body
    descriptor = body.get("descriptor")
    assert isinstance(descriptor, dict), body
    assert descriptor["name"] == "快递代收台账"
    assert descriptor["org_unit_code"] == "FIN"
    assert [f["key"] for f in descriptor["fields"]] == ["title", "status"]
    assert body["can_base"] is True
    # 打不开这一页，界面与 AI 都要能看出这一点。
    assert body["can_read"] is False


def test_reference_right_does_not_open_the_entries(world):
    """参考只给定义：拿到参考权的票去读条目，必须 403。"""

    token = _ticket(world, "GA001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET",
                         f"/v1/interfaces/{world['pages'][SHARED_SLUG]}/entries", token=token)
    assert status == 403, body


def test_a_page_not_shared_with_you_is_still_refused(world):
    token = _ticket(world, "GA001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET",
                         f"/v1/interfaces/{world['pages'][PRIVATE_SLUG]}", token=token)
    assert status == 403, body


def test_the_descriptor_still_needs_an_app_scope(world):
    """票上没有建页/读条目这两档，就不该靠参考权白拿定义。"""

    token = _ticket(world, "GA001", [])
    status, body = _call(world["base"], "GET",
                         f"/v1/interfaces/{world['pages'][SHARED_SLUG]}", token=token)
    assert status == 403, body


def test_the_owner_still_reads_their_own_page(world):
    token = _ticket(world, "FIN001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET",
                         f"/v1/interfaces/{world['pages'][PRIVATE_SLUG]}", token=token)
    assert status == 200, body
    assert body["can_read"] is True
    assert body["descriptor"]["name"] == "报销明细台账"


# ---------------------------------------------------------------------------
# ② AI 的手上要有这个动作
# ---------------------------------------------------------------------------


def _live_tool(world, token: str, monkeypatch, name: str):
    ticket_file = Path("/tmp") / f"oams-ref-{name}-{abs(hash(world['base']))}.json"
    ticket_file.write_text(json.dumps({"token": token}), encoding="utf-8")
    monkeypatch.setenv("OAMS_BASE_URL", world["base"])
    monkeypatch.setenv("OAMS_TOKEN_FILE", str(ticket_file))

    from oams.mcp import live as live_module

    return live_module.LIVE_TOOLS_BY_NAME[name]


def test_the_live_tool_hands_the_definition_to_the_ai(world, monkeypatch):
    token = _ticket(world, "GA001", BUILD_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_page_definition")
    result = spec.handler(None, {"interface_id": world["pages"][SHARED_SLUG]})
    body = result.to_dict()
    assert result.ok, json.dumps(body, ensure_ascii=False)[:800]
    assert body["descriptor"]["name"] == "快递代收台账"
    assert body["can_read"] is False
    text = json.dumps(body, ensure_ascii=False)
    assert "参考" in text, "只有参考权时得说清：定义给你、条目不给"
    assert "条目内容不给" in text, text[:600]


def test_the_live_tool_says_which_page_it_cannot_reach(world, monkeypatch):
    token = _ticket(world, "GA001", BUILD_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_page_definition")
    result = spec.handler(None, {"interface_id": world["pages"][PRIVATE_SLUG]})
    body = result.to_dict()
    assert not result.ok, body
    assert body.get("status") == 403, body


# ---------------------------------------------------------------------------
# ③ 起点要能被发现：AI 拿到票之后，怎么知道有哪一页可以照着做
# ---------------------------------------------------------------------------


def test_me_lists_the_pages_others_opened_to_you(world):
    """不列出来，AI 就只能等人告诉它页面编号——这条路的第一步就断了。"""

    token = _ticket(world, "GA001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET", "/v1/me", token=token)
    assert status == 200, body
    listed = {page["slug"]: page for page in body["reference_pages"]}
    # 三格全假的那一页不算：清单上是「能拿去用的页」，不是「表格里出现过的行」。
    assert set(listed) == {SHARED_SLUG}, body["reference_pages"]
    page = listed[SHARED_SLUG]
    assert page["interface_id"] == world["pages"][SHARED_SLUG]
    assert page["can_base"] is True
    # 只有参考权：条目看不到，这一点必须一起报出来，别让 AI 以为能读数据。
    assert page["can_read"] is False
    assert page["from_org_code"] == "FIN"
    # 没开给它的页不在清单里。
    assert PRIVATE_SLUG not in listed


def test_me_does_not_call_your_own_pages_a_reference(world):
    """自己部门的页能看能填，不是「别人开给你的参考」，混进来会让 AI 反复照抄自己。"""

    token = _ticket(world, "FIN001", BUILD_SCOPES)
    status, body = _call(world["base"], "GET", "/v1/me", token=token)
    assert status == 200, body
    assert body["reference_pages"] == [], body["reference_pages"]


def test_departments_tool_points_at_the_reference_page(world, monkeypatch):
    """AI 的第一声问话里就得有它：哪一页可以照着做、用哪个编号去取定义。"""

    token = _ticket(world, "GA001", BUILD_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_departments")
    result = spec.handler(None, {})
    body = result.to_dict()
    assert result.ok, json.dumps(body, ensure_ascii=False)[:800]
    summary = body["summary"]
    assert SHARED_SLUG in summary, summary
    assert world["pages"][SHARED_SLUG] in summary, summary
    assert "条目看不到" in summary, summary
    assert [p["slug"] for p in body["reference_pages"]] == [SHARED_SLUG]
