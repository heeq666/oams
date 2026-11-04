"""三个走 HTTP 的 MCP 工具，以及它们背后那两个接口。

工具本身不连数据库：它带着应用票调 `/v1`，跟外部 AI 自己用 curl 是同一套规矩。
所以这里也起一个真站点、拿一张真票，不 mock——mock 掉的那一层正是要验的。

三条边界最要紧：
- 没票、没配地址、票过期，都要说人话，**不许静默退回沙盒**；
- 坏描述符要指出是哪一条、在哪个位置、怎么改，而不是一个 traceback；
- 送审成不成看回读到的 status，`POST` 返回 200 不算。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path
import test_connected_apps as edges  # noqa: E402  同一套真客户端与建票流程

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[1]
GA_DESCRIPTOR = json.loads(
    (REPO / "data" / "neutral-demo-ga-parcel-lookup.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# 一个自己的世界：库、公司管理员、GA 部，外加一个「部门里但不是作者」的人
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(db_ready):
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
                """INSERT INTO oams.positions (position_code, title, org_unit_id)
                   SELECT 'GA_LEAD', '行政负责人', org_unit_id
                     FROM oams.org_units WHERE org_code = 'GA'"""
            )
            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'"
            ).fetchone()[0]
        colleague = _sql(
            url,
            """WITH baru AS (
                   INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES ('GA001', '行政同事', 'active')
                   RETURNING member_id)
               INSERT INTO oams.position_assignments
                      (member_id, position_id, assignment_type, status, starts_at, reason)
               SELECT baru.member_id, p.position_id, 'primary', 'active', now() - interval '1 day',
                      '用例：给 GA 部挂一个不是作者的人'
                 FROM baru, oams.positions p WHERE p.position_code = 'GA_LEAD'
               RETURNING member_id""",
        )
        colleague_id = str(colleague[0]["member_id"])
        _sql(
            url,
            """INSERT INTO oams.permission_grants
                   (subject_type, member_id, resource, action, scope_type, valid_until, reason,
                    approved_by, approved_at)
               SELECT 'member', %s, 'workforce.reports', wanted.action, 'all',
                      now() + interval '30 days', '用例：填报的读写，不给公司级范围',
                      %s, now()
                 FROM (VALUES ('read'), ('submit')) AS wanted(action)""",
            (colleague_id, colleague_id),
        )
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "admin_member_id": str(admin_id),
                   "colleague_member_id": colleague_id}
    finally:
        suite._drop_database(db_ready, name)


def _sql(url: str, sql: str, args=()):
    """直接改这个世界里的行（例如把页推到 active），走自己的连接。"""

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cursor = conn.execute(sql, args)
        rows = cursor.fetchall() if cursor.description is not None else []
        conn.commit()
    return rows


def _get_json(base: str, path: str, token: str) -> tuple[int, object]:
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, edges._decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, edges._decode(error.read())


def _session_token(member_id: str) -> str:
    """给某个人签一张普通会话票（不是应用票）：验「谁在送」时用它最直接。"""

    from oams.security import issue_session

    token, _ = issue_session(member_id)
    return token


def _app_ticket(world, scopes: list[str] | None = None) -> dict:
    scopes = scopes or ["interfaces:draft"]
    code = edges._issue_code(world, scopes, name="用例应用")
    assert edges._allow(world, code["code"], scopes) in (302, 303)
    status, ticket = edges._exchange(world, code["code"])
    assert status == 200, ticket
    ticket.setdefault("app_id", code["app_id"])
    ticket.setdefault("scopes", scopes)
    return ticket


def _submit(world, page: dict, token: str) -> tuple[int, object]:
    return edges._post_json(world["base"], f"/v1/interfaces/{page['interface_id']}/submit",
                            None, token=token)


def _status_of(world, interface_id: str) -> str:
    rows = _sql(world["url"], "SELECT status FROM oams.interfaces WHERE interface_id = %s",
                (interface_id,))
    return rows[0]["status"]


def _audit_rows(world, interface_id: str) -> list[dict]:
    return _sql(
        world["url"],
        """SELECT event_type, reason, metadata FROM oams.workforce_audit_events
            WHERE subject_id = %s ORDER BY occurred_at""",
        (interface_id,),
    )


# ---------------------------------------------------------------------------
# ① 送审：草稿 -> pending，拒绝的话每句都得是人话
# ---------------------------------------------------------------------------


def test_submitting_a_draft_puts_it_in_the_department_queue(world):
    ticket = _app_ticket(world, ["interfaces:draft"])
    page = edges._create_page(world, "mcp-submit-" + uuid.uuid4().hex[:6])

    status, body = _submit(world, page, ticket["token"])
    assert status == 200, body
    assert body["status"] == "pending"
    assert body["review_stage"] == "department"
    assert _status_of(world, page["interface_id"]) == "pending"

    rows = [row for row in _audit_rows(world, page["interface_id"])
            if row["event_type"] == "interface.review_requested"]
    assert rows, "送审没有留下审计"
    assert "提交了页面送审" in rows[-1]["reason"]
    assert (rows[-1]["metadata"] or {}).get("app_id") == ticket["app_id"]


def test_resubmitting_a_pending_page_is_a_409_that_says_why(world):
    ticket = _app_ticket(world, ["interfaces:draft"])
    page = edges._create_page(world, "mcp-twice-" + uuid.uuid4().hex[:6])
    assert _submit(world, page, ticket["token"])[0] == 200

    status, body = _submit(world, page, ticket["token"])
    assert status == 409, body
    assert "已经提交" in body["error"] and "等审核" in body["error"]
    assert _status_of(world, page["interface_id"]) == "pending", "被拒的那次不许改状态"


def test_an_active_page_cannot_be_sent_again(world):
    ticket = _app_ticket(world, ["interfaces:draft"])
    page = edges._create_page(world, "mcp-active-" + uuid.uuid4().hex[:6])
    # 直接改库到 active：这条验的是送审的拒绝，不是审核流程本身
    _sql(world["url"], "UPDATE oams.interfaces SET status = 'active' WHERE interface_id = %s",
         (page["interface_id"],))

    status, body = _submit(world, page, ticket["token"])
    assert status == 409, body
    assert "已经启用" in body["error"]
    assert _status_of(world, page["interface_id"]) == "active"


def test_a_colleague_in_the_same_department_cannot_submit_your_page(world):
    page = edges._create_page(world, "mcp-author-" + uuid.uuid4().hex[:6])

    colleague = _session_token(world["colleague_member_id"])
    status, body = _submit(world, page, colleague)
    assert status == 403, body
    assert "不是你建的" in (body.get("detail") or body.get("error") or ""), body
    assert _status_of(world, page["interface_id"]) == "draft", "别人的手不许把页推上去"

    # 作者自己送得成——不然那不是「限制替别人」，是「谁都送不了」
    author = _session_token(world["admin_member_id"])
    status, body = _submit(world, page, author)
    assert status == 200, body
    assert _status_of(world, page["interface_id"]) == "pending"


# ---------------------------------------------------------------------------
# ② /v1/me：这个人能给哪些部门建页（岗位码也只能从这里拿）
# ---------------------------------------------------------------------------


def test_a_company_admin_can_build_in_every_department(world):
    token = _session_token(world["admin_member_id"])
    status, body = _get_json(world["base"], "/v1/me", token)
    assert status == 200, body
    assert body["writable_all"] is True
    codes = [unit["org_code"] for unit in body["writable_org_units"]]
    assert "GA" in codes and "COMPANY" in codes, codes
    ga = next(unit for unit in body["writable_org_units"] if unit["org_code"] == "GA")
    assert [position["position_code"] for position in ga["positions"]] == ["GA_LEAD"]
    assert "公司级" in body["writable_note"]


def test_a_department_member_only_gets_their_own_department(world):
    token = _session_token(world["colleague_member_id"])
    status, body = _get_json(world["base"], "/v1/me", token)
    assert status == 200, body
    assert body["writable_all"] is False
    assert [unit["org_code"] for unit in body["writable_org_units"]] == ["GA"]
    assert "只能给这些部门建页" in body["writable_note"] and "GA" in body["writable_note"]


# ---------------------------------------------------------------------------
# ③ 三个工具：没票不降级、坏描述符说人话、送审后回读
# ---------------------------------------------------------------------------


def _tool_client(world, monkeypatch, token_file: Path) -> None:
    """把工具指到这个站点与这张票文件上。"""

    monkeypatch.setenv("OAMS_BASE_URL", world["base"])
    monkeypatch.setenv("OAMS_TOKEN_FILE", str(token_file))


def _write_token(path: Path, token: str, days: int = 1) -> Path:
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    path.write_text(json.dumps({"token": token, "expires_at": expires.isoformat()}))
    return path


def _call(name: str, arguments: dict | None = None):
    from oams.mcp.live import build_live_registry

    return build_live_registry().call(name, arguments or {})


def test_the_tools_say_where_to_get_a_ticket_instead_of_falling_back(world, monkeypatch, tmp_path):
    _tool_client(world, monkeypatch, tmp_path / "no-such-token.json")
    result = _call("oams_departments")
    assert result.ok is False
    assert result.error_code == "LIVE_CONFIG_ERROR"
    message = result.payload["message"]
    assert "还没拿到票" in message
    assert "skills/oams-connect" in message, "要告诉人去哪拿票"

    monkeypatch.delenv("OAMS_BASE_URL", raising=False)
    result = _call("oams_departments")
    assert result.ok is False and result.error_code == "LIVE_CONFIG_ERROR"
    assert "OAMS_BASE_URL" in result.payload["message"]


def test_an_expired_ticket_says_so_and_does_not_pretend(world, monkeypatch, tmp_path):
    token = _session_token(world["admin_member_id"])
    path = _write_token(tmp_path / "expired.json", token, days=-1)
    _tool_client(world, monkeypatch, path)
    result = _call("oams_departments")
    assert result.ok is False and result.error_code == "LIVE_CONFIG_ERROR"
    assert "到期" in result.payload["message"]


def test_check_descriptor_explains_three_kinds_of_mistakes_in_chinese(world, monkeypatch, tmp_path):
    path = _write_token(tmp_path / "token.json", _session_token(world["admin_member_id"]))
    _tool_client(world, monkeypatch, path)

    unknown_codes = dict(GA_DESCRIPTOR, org_unit_code="NOPE", position_codes=["NOPE_LEAD"])
    result = _call("oams_check_descriptor", {"descriptor": unknown_codes})
    assert result.ok is False and result.error_code == "DESCRIPTOR_INVALID"
    problems = "\n".join(result.payload["problems"])
    assert "不在你可建的部门里" in problems, problems
    assert "不在这个部门里" in problems, problems

    bad_module = json.loads(json.dumps(GA_DESCRIPTOR))
    bad_module["modules"] = [dict(bad_module["modules"][0], type="flying_carousel")]
    result = _call("oams_check_descriptor", {"descriptor": bad_module})
    assert result.ok is False
    assert "不在白名单里" in "\n".join(result.payload["problems"])

    no_map_to = json.loads(json.dumps(GA_DESCRIPTOR))
    no_map_to["fields"][0].pop("map_to", None)
    result = _call("oams_check_descriptor", {"descriptor": no_map_to})
    assert result.ok is False
    assert "缺 map_to" in "\n".join(result.payload["problems"])


def test_submit_page_builds_a_draft_and_reads_the_status_back(world, monkeypatch, tmp_path):
    token = _session_token(world["admin_member_id"])
    _tool_client(world, monkeypatch, _write_token(tmp_path / "token.json", token))
    slug = "mcp-tool-" + uuid.uuid4().hex[:6]

    result = _call("oams_submit_page",
                   {"slug": slug, "name": "工具建的页", "descriptor": GA_DESCRIPTOR})
    assert result.ok is True, result.payload
    interface_id = result.payload["interface_id"]
    assert result.payload["verified_status"] == "pending"
    assert "回读确认" in result.payload["summary"]
    assert _status_of(world, interface_id) == "pending"
    rows = [row for row in _audit_rows(world, interface_id)
            if row["event_type"] == "interface.review_requested"]
    assert rows, "工具送审也要在审计里留痕"
