"""接入方的票替人报进度：MCP 填报通道的那道闸。

v1 要验的核心假设是「AI 把填报成本压到接近零」。压到零的前提是 AI 真能把数据
写进去；而写进去之后，看板上必须分得出**这一条是人填的还是 AI 代笔的**——那是
整个项目的成绩单。

所以这个文件里最要紧的不是「能报进度」，是第二条：**票不能自己说自己是人。**
submitted_via 以前由客户端传，谁都会说自己填的，于是那张成绩单等于没有。现在
由服务端按「这张票是不是接入方换来的」判，这里把它钉死。

同时把另一头也钉住：**票不能建工作项**。工作项是组织对「该做什么」的判断，
放进票里，看板就会长出一个 AI 自己想象出来的任务清单。
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

#: 报进度需要的两个范围：读本部门工作项、替人申报进度。
REPORT_SCOPES = ["work_items:read:dept", "reports:submit"]


@pytest.fixture(scope="module")
def world(db_ready):
    """一个自己的库：管理员 + 一个 GA 部门 + 一个岗位 + 一个成员 + 两条工作项。

    不靠主种子：这里要的是「一个人坐在一个部门岗位上」，别的东西越少越好数。
    """

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
            conn.execute(
                """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                   SELECT 'FIN', '财务部', 'department', org_unit_id
                     FROM oams.org_units WHERE org_code = 'COMPANY'"""
            )
            conn.execute(
                """INSERT INTO oams.positions (position_code, title, org_unit_id)
                   SELECT 'GA_LEAD', '行政负责人', org_unit_id
                     FROM oams.org_units WHERE org_code = 'GA'"""
            )
            conn.execute(
                """INSERT INTO oams.positions (position_code, title, org_unit_id)
                   SELECT 'FIN_LEAD', '财务负责人', org_unit_id
                     FROM oams.org_units WHERE org_code = 'FIN'"""
            )
            # 部门负责人的权全部发成 `all` 档——跟开发库 55433 的真实形状一致；
            # 座位不在公司本级，所以照样只看得到自己部门（见 test_read_scope.py）。
            conn.execute(
                """INSERT INTO oams.permission_grants
                       (subject_type, position_id, resource, action, scope_type, valid_from, reason)
                   SELECT 'position', p.position_id, g.resource, g.action, 'all', now(), 'seed'
                     FROM oams.positions p
                     JOIN (VALUES ('workforce.work_items', 'read'),
                                  ('workforce.reports', 'read'),
                                  ('workforce.reports', 'submit')) AS g(resource, action) ON true
                    WHERE p.position_code IN ('GA_LEAD', 'FIN_LEAD')"""
            )
            conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status, joined_at)
                   VALUES ('GA001', '行政小兵', 'active', current_date - 30)"""
            )
            conn.execute(
                """INSERT INTO oams.position_assignments
                       (member_id, position_id, assignment_type, status, starts_at, reason)
                   SELECT m.member_id, p.position_id, 'primary', 'active', now() - interval '30 days', 'seed'
                     FROM oams.workforce_members m, oams.positions p
                    WHERE m.employee_code = 'GA001' AND p.position_code = 'GA_LEAD'"""
            )
            conn.execute(
                """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
                   SELECT p.org_unit_id, p.position_id, '整理办公用品盘点表', 'routine', 'in_progress'
                     FROM oams.positions p WHERE p.position_code = 'GA_LEAD'"""
            )
            conn.execute(
                """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
                   SELECT p.org_unit_id, p.position_id, '核对季度报销凭证', 'routine', 'in_progress'
                     FROM oams.positions p WHERE p.position_code = 'FIN_LEAD'"""
            )
            rows = conn.execute(
                "SELECT employee_code, member_id FROM oams.workforce_members"
            ).fetchall()
            members = {code: str(member_id) for code, member_id in rows}
            items = {
                title: str(item_id)
                for title, item_id in conn.execute(
                    "SELECT title, work_item_id FROM oams.work_items").fetchall()
            }
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "members": members, "items": items}
    finally:
        suite._drop_database(db_ready, name)


def _ticket(world, member_code: str, scopes: list[str]) -> str:
    """代表这个人换一张票：码由应用自己领，允许由本人点。"""

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


def _stored_via(world, report_id) -> str:
    """不看接口回什么，直接看库里那一行。"""

    import psycopg

    with psycopg.connect(world["url"]) as conn:
        return conn.execute(
            "SELECT submitted_via FROM oams.work_item_reports WHERE report_id = %s",
            (report_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# ① 能填：票把进度记进去，且库里记的是 agent
# ---------------------------------------------------------------------------


def test_a_ticket_reports_progress_and_the_row_says_agent(world):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    item = world["items"]["整理办公用品盘点表"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"progress_value": 40, "note": "盘点到三楼",
                          "evidence": "盘点表 v2，行政部三楼柜位已核完"}, token)
    assert status == 201, body
    # 回什么不算，库里那一行才算。
    assert _stored_via(world, body["report_id"]) == "agent"


def test_a_ticket_cannot_call_itself_human(world):
    """本次最重要的一条：票在请求里硬塞 submitted_via=human，库里仍须是 agent。

    这是那张成绩单的唯一一道防线。它红了就说明「AI 到底替人填了多少」这个数
    又能被客户端编了。
    """

    token = _ticket(world, "GA001", REPORT_SCOPES)
    item = world["items"]["整理办公用品盘点表"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"submitted_via": "human", "progress_value": 60,
                          "evidence": "把别人写好的进度抄进来"}, token)
    assert status == 201, body
    assert _stored_via(world, body["report_id"]) == "agent", body


def test_a_human_session_is_still_recorded_as_human(world):
    """反向对照：人在网页上点的那条，不能被记成 agent——不然成绩单是反的。"""

    item = world["items"]["整理办公用品盘点表"]
    admin_id = world["members"]["ADMIN001"]
    from oams.security import issue_session

    token, _ = issue_session(admin_id)
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"progress_value": 70, "evidence": "管理员在网页上补的"},
                         token=token)
    assert status == 201, body
    assert _stored_via(world, body["report_id"]) == "human"


# ---------------------------------------------------------------------------
# ② 填不进去的那几扇门
# ---------------------------------------------------------------------------


def test_without_the_report_scope_the_ticket_is_refused(world):
    token = _ticket(world, "GA001", ["work_items:read:dept"])
    item = world["items"]["整理办公用品盘点表"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"evidence": "只申请了读的票想写"}, token)
    assert status == 403, body


def test_a_ticket_cannot_create_a_work_item(world):
    """工作项是组织对「该做什么」的判断，AI 不能自己造任务。"""

    token = _ticket(world, "GA001", REPORT_SCOPES)
    status, body = _call(world["base"], "POST", "/v1/work-items",
                         {"title": "AI 自己发明的一件事"}, token)
    assert status == 403, body
    assert "这张票没有被允许做这件事" in body["detail"], body


def test_cannot_report_on_another_departments_item(world):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    item = world["items"]["核对季度报销凭证"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"evidence": "替别的部门报进度"}, token)
    assert status == 403, body


def test_evidence_is_required(world):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    item = world["items"]["整理办公用品盘点表"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"progress_value": 30, "evidence": "   "}, token)
    assert status == 422, body


def test_progress_outside_zero_to_hundred_is_refused(world):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    item = world["items"]["整理办公用品盘点表"]
    status, body = _call(world["base"], "POST", f"/v1/work-items/{item}/reports",
                         {"progress_value": 120, "evidence": "编的百分数"}, token)
    assert status == 422, body


# ---------------------------------------------------------------------------
# ③ 我的工作项：只列我坐着的岗位上、还开着的事
# ---------------------------------------------------------------------------


def test_my_work_items_lists_only_my_positions(world):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    status, body = _call(world["base"], "GET", "/v1/work-items?mine=true&open_only=true",
                         token=token)
    assert status == 200, body
    titles = [item["title"] for item in body]
    assert "整理办公用品盘点表" in titles, titles
    assert "核对季度报销凭证" not in titles, titles


def test_my_work_items_is_behind_a_scope(world):
    """没有 work_items:read:dept 的票，连列表都不给它看。"""

    token = _ticket(world, "GA001", ["reports:submit"])
    status, body = _call(world["base"], "GET", "/v1/work-items?mine=true", token=token)
    assert status == 403, body


# ---------------------------------------------------------------------------
# ④ MCP 那一层：真连站点，写完回读
# ---------------------------------------------------------------------------


def _live_tool(world, token: str, monkeypatch, name: str):
    """把工具指到这个测试站点和这张票上，然后拿真的 handler 跑。"""

    ticket_file = Path("/tmp") / f"oams-ticket-{name}-{abs(hash(world['base']))}.json"
    ticket_file.write_text(json.dumps({"token": token}), encoding="utf-8")
    monkeypatch.setenv("OAMS_BASE_URL", world["base"])
    monkeypatch.setenv("OAMS_TOKEN_FILE", str(ticket_file))

    from oams.mcp import live as live_module

    return live_module.LIVE_TOOLS_BY_NAME[name]


def test_the_live_tool_lists_my_work_items(world, monkeypatch):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_my_work_items")
    result = spec.handler(None, {})
    assert result.ok, result.to_dict()
    text = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "整理办公用品盘点表" in text, text[:600]
    assert "核对季度报销凭证" not in text, text[:600]


def test_the_live_tool_writes_progress_and_reads_it_back(world, monkeypatch):
    """站点说成不算成：工具必须回读一次，并按回读到的那个 submitted_via 说话。"""

    token = _ticket(world, "GA001", REPORT_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_submit_progress")
    item = world["items"]["整理办公用品盘点表"]
    result = spec.handler(None, {
        "work_item_id": item,
        "progress_value": 80,
        "note": "盘点收尾",
        "evidence": "盘点表 v3，全部柜位已核",
    })
    body = result.to_dict()
    assert result.ok, json.dumps(body, ensure_ascii=False)[:800]
    assert body.get("verified") is True, body
    assert body.get("submitted_via") == "agent", body
    # 库里那一行也要对得上，别只信工具自己的话。
    assert _stored_via(world, body["report_id"]) == "agent"


def test_the_live_tool_refuses_empty_evidence_without_touching_the_site(world, monkeypatch):
    token = _ticket(world, "GA001", REPORT_SCOPES)
    spec = _live_tool(world, token, monkeypatch, "oams_submit_progress")
    item = world["items"]["整理办公用品盘点表"]
    result = spec.handler(None, {"work_item_id": item, "evidence": "  "})
    assert not result.ok, result.to_dict()
    assert result.error_code, result.to_dict()
