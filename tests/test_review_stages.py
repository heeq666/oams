"""两级审核：部门先过，公司再终审 —— 谁审谁、队列只给能审的人、作者不审自己。

一轮里三件事必须同时成立，否则这一层等于没加：
* 部门负责人只看得到、也只动得了自己部门的**初审**页，别的部门、公司终审都进不来；
* 平台（系统管理员）看得到公司终审页，也看得到全公司在等什么，但只读——初审归部门；
* 自己提交的页面别人审，谁都不能批自己交的那一版；
* 初审通过之后，只给自己用的页立刻上线，而向外/进公司汇总的页仍在待审，只是换了级别；
  这一趟不算「给了意见」，所以 `reviewed_by`/`reviewed_at` 不动，页子仍然可以撤回。

用例走 TestClient + 种子库，整笔事务在结束时回滚，库里不留痕迹。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import uuid

import pytest
from starlette.testclient import TestClient

import workhub.api
from workhub import review as review_rules
from workhub import web
from workhub.config import settings

pytestmark = pytest.mark.db

DESIGN_REVIEWER = "DESIGN001"   # 设计部负责人：有 review，没有 review_company
PLATFORM = "ADMIN001"           # 系统管理员：有 review_company，兜底全公司
AUTHOR = "MVP1002"              # 一个不审任何东西的人，用来当作者

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "review-stage-sample",
    "name": "审核分级样张",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "layout", "type": "rows",
                 "children": [{"key": "progress", "type": "progress_row",
                               "source_fields": ["title", "state_word"]}]}],
    "fields": [
        {"key": "title", "label": "款号", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "options": ["进行中", "已完成"]},
    ],
    "status_map": {"进行中": "in_progress", "已完成": "done"},
    "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
             "visibility": "department", "review": {"status": "draft"}},
}


# ---------------------------------------------------------------- 纯逻辑部分


def test_a_public_page_never_gets_settled_by_its_own_department():
    assert review_rules.needs_company_review(_page(visibility="public")) is True


def test_a_company_figure_page_goes_up_one_level():
    descriptor = _page()
    descriptor["capabilities"] = ["entries:write", "aggregate:read:company"]
    assert review_rules.needs_company_review(descriptor) is True


def test_a_page_written_on_another_departments_work_goes_up_one_level():
    assert review_rules.needs_company_review(_page(), cross_department=True) is True


def test_an_inward_facing_page_is_the_departments_own_decision():
    assert review_rules.needs_company_review(_page()) is False
    assert review_rules.needs_company_review(None) is False


def _page(**meta):
    descriptor = json.loads(json.dumps(DESCRIPTOR))
    descriptor["meta"].update(meta)
    return descriptor


# ---------------------------------------------------------------- 站点部分


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


def _token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"workhub:review-form:v1", hashlib.sha256).hexdigest()


@pytest.fixture()
def room(seeded_database, monkeypatch):
    """一笔事务里的种子库 + 一个能按身份切人的 TestClient。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(workhub.api, "connection", lambda: _one_transaction(conn))

    people = {code: seeded_database.member_id(code)
              for code in (DESIGN_REVIEWER, PLATFORM, AUTHOR)}
    design = seeded_database.org_unit_id("DESIGN")
    hr = seeded_database.org_unit_id("HR")
    state = {"me": DESIGN_REVIEWER, "cookie": "review-stage-test"}
    monkeypatch.setattr(workhub.api, "principal_from_request",
                        lambda request: {"member_id": people[state["me"]],
                                         "employee_code": state["me"],
                                         "display_name": state["me"],
                                         "employment_status": "active"})

    def client() -> TestClient:
        made = TestClient(workhub.api.app)
        made.cookies.set(settings.web_session_cookie, state["cookie"])
        return made

    def page(org, *, stage="department", descriptor=None, author=AUTHOR, status="pending"):
        interface_id = uuid.uuid4()
        conn.execute(
            """INSERT INTO workhub.interfaces
                 (interface_id, org_unit_id, slug, name, status, visibility, schema_version,
                  descriptor, version_no, created_by, updated_by, review_stage)
               VALUES (%s, %s, %s, %s, %s, 'department', '0.2', %s::jsonb, 1, %s, %s, %s)""",
            (interface_id, org, f"review-stage-{interface_id.hex[:8]}", "审核分级样张",
             status, json.dumps(descriptor or DESCRIPTOR), people[author],
             people[author], stage))
        return interface_id

    try:
        yield {"conn": conn, "client": client, "as_": lambda code: state.update(me=code),
               "token": _token(state["cookie"]), "page": page, "design": design, "hr": hr,
               "people": people}
    finally:
        conn.rollback()
        conn.close()


def _approve(room, interface_id, note=""):
    return room["client"]().post(f"/reviews/{interface_id}/approve",
                                 data={"csrf": room["token"], "note": note},
                                 follow_redirects=False)


def _reject(room, interface_id, note="改一下字段", code="FIELD_UNMAPPED"):
    return room["client"]().post(f"/reviews/{interface_id}/reject",
                                 data={"csrf": room["token"], "note": note,
                                       "reason_code": code},
                                 follow_redirects=False)


def _row(room, interface_id):
    return room["conn"].execute(
        """SELECT status, review_stage, reviewed_at, reviewed_by, visibility
             FROM workhub.interfaces WHERE interface_id = %s""",
        (interface_id,)).fetchone()


def test_a_department_lead_sees_only_their_own_first_level_pages(room):
    mine = room["page"](room["design"])
    elsewhere = room["page"](room["hr"])
    at_company = room["page"](room["design"], stage="company")

    room["as_"](DESIGN_REVIEWER)
    body = room["client"]().get("/reviews").text

    assert f"/reviews/{mine}/approve" in body
    assert f"/reviews/{elsewhere}/approve" not in body
    # 公司终审不是他的活，连按钮都不该出现
    assert f"/reviews/{at_company}/approve" not in body


def test_nobody_reviews_the_version_they_submitted(room):
    authored_by_reviewer = room["page"](room["design"], author=DESIGN_REVIEWER)
    room["as_"](DESIGN_REVIEWER)
    body = room["client"]().get("/reviews").text

    assert f"/reviews/{authored_by_reviewer}/approve" not in body


def test_the_platform_sees_the_final_stage_and_a_read_only_view_of_the_rest(room):
    at_company = room["page"](room["design"], stage="company")
    still_department = room["page"](room["design"])

    room["as_"](PLATFORM)
    body = room["client"]().get("/reviews").text

    assert f"/reviews/{at_company}/approve" in body
    # 部门初审那页只读地列出来，按钮不给
    assert f"/reviews/{still_department}/approve" not in body
    assert "别处还在等初审" in body


def test_an_inward_page_goes_live_on_the_departments_own_yes(room):
    page = room["page"](room["design"])
    room["as_"](DESIGN_REVIEWER)
    _approve(room, page, note="可以")

    row = _row(room, page)
    assert row["status"] == "active"
    assert row["reviewed_by"] == room["people"][DESIGN_REVIEWER]


def test_a_public_page_stops_at_the_company_stage_after_the_department_agrees(room):
    page = room["page"](room["design"], descriptor=_page(visibility="public"))
    room["as_"](DESIGN_REVIEWER)
    _approve(room, page, note="部门过")

    row = _row(room, page)
    # 还在待审，只是换了级别；这一趟不是「给了意见」，所以没写 reviewed_*
    assert (row["status"], row["review_stage"]) == ("pending", "company")
    assert row["reviewed_at"] is None
    audit = room["conn"].execute(
        """SELECT count(*) AS n FROM workhub.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.review_stage_passed'""",
        (page,)).fetchone()
    assert audit["n"] == 1

    room["as_"](PLATFORM)
    _approve(room, page, note="公司过")
    row = _row(room, page)
    assert (row["status"], row["visibility"]) == ("active", "public")


def test_a_department_lead_cannot_settle_the_final_stage(room):
    page = room["page"](room["design"], stage="company")
    room["as_"](DESIGN_REVIEWER)
    response = _approve(room, page)

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    row = _row(room, page)
    assert (row["status"], row["review_stage"]) == ("pending", "company")


def test_a_return_starts_the_page_over_at_the_first_level(room):
    page = room["page"](room["design"], stage="company")
    room["as_"](PLATFORM)
    _reject(room, page, note="字段没接标准字段")

    row = _row(room, page)
    assert (row["status"], row["review_stage"]) == ("draft", "department")
    assert row["reviewed_at"] is not None


def test_the_platform_stands_in_only_when_the_department_has_nobody_left(room):
    """部门唯一的审核人就是提交者时，平台必须能代签第一级，否则这页永远发不出去。

    有别人能审的时候不许代签：那是抢部门的活，不是救场。
    """
    stuck = room["page"](room["design"], author=DESIGN_REVIEWER)
    covered = room["page"](room["design"])

    room["as_"](PLATFORM)
    body = room["client"]().get("/reviews").text
    assert f"/reviews/{stuck}/approve" in body
    assert "平台代审" in body
    assert f"/reviews/{covered}/approve" not in body


def test_a_standing_in_approval_says_so_in_the_audit_record(room):
    page = room["page"](room["design"], author=DESIGN_REVIEWER)

    room["as_"](PLATFORM)
    _approve(room, page, note="部门没人能审，平台代签")

    assert _row(room, page)["status"] == "active"
    payload = room["conn"].execute(
        """SELECT after_state FROM workhub.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.approved'
            ORDER BY occurred_at DESC LIMIT 1""",
        (page,)).fetchone()["after_state"]
    # 记录不能假装部门自己过的
    assert payload["acting"] == "company_proxy"
