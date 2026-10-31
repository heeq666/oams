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
import urllib.parse
import uuid

import pytest
from starlette.testclient import TestClient

import oams.api
from oams import review as review_rules
from oams import web
from oams.config import settings

pytestmark = pytest.mark.db

DESIGN_REVIEWER = "DESIGN001"   # 设计部负责人：有 review，没有 review_company
PLATFORM = "ADMIN001"           # 系统管理员：有 review_company，兜底全公司
DESIGN_AUTHOR = "DESIGN002"     # 设计部设计师：能建页、能提交，审不了（本文件的事务里现造）
HR_AUTHOR = "MVP1001"           # 人事经理：人事部的默认作者，用来造「别的部门」的页

# 作者是谁现在由部门决定：真实入口要求作者本人就在那个部门里（平台身份除外）。
# 种子里 DESIGN 部只有负责人一个，而他是审核人，所以 fixture 里补一个设计师。

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
         "map_to": "title", "aggregatable": True, "example": "AW24-001"},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "example": "进行中",
         "options": ["进行中", "已完成"]},
        {"key": "evidence", "label": "凭据", "type": "text", "required": False,
         "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
    ],
    "status_map": {"进行中": "in_progress", "已完成": "done"},
    "rules": {"evidence_required": False},
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
    return hmac.new(cookie.encode(), b"oams:review-form:v1", hashlib.sha256).hexdigest()


def _page_token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"oams:page-form:v1", hashlib.sha256).hexdigest()


@pytest.fixture()
def room(seeded_database, monkeypatch):
    """一笔事务里的种子库 + 一个能按身份切人的 TestClient。

    待审页一律从真实入口造：作者本人点 `/departments/{org}/new` 建页，再提交审核。
    种子里 DESIGN 部只有负责人一个，而他就是审核人；建页要求作者本人在那个部门里，
    所以这里补一个设计师（只在本次事务里存在，结束时回滚）。补的是人，不是待审页——
    页子仍然一张一张走流程，`review_stage` 由谁批出来，路由说了算。
    """

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    conn.execute(
        """INSERT INTO oams.positions (position_code, title, org_unit_id)
           SELECT 'DESIGNER', '设计师', org_unit_id FROM oams.org_units
            WHERE org_code = 'DESIGN'""")
    conn.execute(
        """INSERT INTO oams.workforce_members
                 (employee_code, display_name, employment_status, joined_at)
           VALUES (%s, '设计师小李', 'active', current_date - 30)""", (DESIGN_AUTHOR,))
    conn.execute(
        """INSERT INTO oams.position_assignments
                 (member_id, position_id, assignment_type, status, starts_at, reason)
           SELECT m.member_id, p.position_id, 'primary', 'active',
                  now() - interval '30 days', 'seed'
             FROM oams.workforce_members m, oams.positions p
            WHERE m.employee_code = %s AND p.position_code = 'DESIGNER'""", (DESIGN_AUTHOR,))
    # 设计师能建页、能提交，但审不了：审核权留在负责人手里，这一层才有意义。
    conn.execute(
        """INSERT INTO oams.permission_grants
                 (subject_type, position_id, resource, action, scope_type, valid_from, reason)
           SELECT 'position', p.position_id, g.resource, g.action, 'all', now(), 'seed'
             FROM oams.positions p
             JOIN (VALUES ('workforce.org_units', 'read'),
                          ('workforce.positions', 'read'),
                          ('workforce.work_items', 'read'),
                          ('workforce.reports', 'read'),
                          ('workforce.reports', 'submit')) AS g(resource, action)
               ON p.position_code = 'DESIGNER'""")

    design = seeded_database.org_unit_id("DESIGN")
    hr = seeded_database.org_unit_id("HR")
    # 同一条连接查：刚补的设计师还没提交，别的连接看不见。
    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (DESIGN_REVIEWER, PLATFORM, DESIGN_AUTHOR, HR_AUTHOR)}
    state = {"me": DESIGN_REVIEWER, "cookie": "review-stage-test"}
    monkeypatch.setattr(oams.api, "principal_from_request",
                        lambda request: {"member_id": people[state["me"]],
                                         "employee_code": state["me"],
                                         "display_name": state["me"],
                                         "employment_status": "active"})

    def client() -> TestClient:
        made = TestClient(oams.api.app)
        made.cookies.set(settings.web_session_cookie, state["cookie"])
        return made

    def as_(code):
        state["me"] = code

    def author_for(org):
        """这个部门里谁来当作者：平台身份哪个部门都进得去，所以兜底给他。"""
        return {design: DESIGN_AUTHOR, hr: HR_AUTHOR}.get(org, PLATFORM)

    def create(org, *, descriptor=None, author=None, name="审核分级样张", slug=None):
        """在部门工作台里新建一页，返回它的 interface_id。"""
        who = author or author_for(org)
        slug = slug or f"review-stage-{uuid.uuid4().hex[:8]}"
        as_(who)
        made = client().post(f"/departments/{org}/new",
                             data={"csrf": _page_token(state["cookie"]), "slug": slug,
                                   "name": name,
                                   "descriptor_text": json.dumps(descriptor or DESCRIPTOR)},
                             follow_redirects=False)
        assert made.status_code == 303, made.text
        location = urllib.parse.unquote(made.headers["location"])
        assert "error=" not in location, location
        return uuid.UUID(location.split("?")[0].rsplit("/", 1)[-1])

    def submit(org, interface_id):
        """作者把页子交上去：draft → pending（部门初审）。"""
        handed = client().post(f"/departments/{org}/{interface_id}/submit-review",
                               data={"review_csrf": _page_token(state["cookie"])},
                               follow_redirects=False)
        assert handed.status_code == 303, handed.text
        location = urllib.parse.unquote(handed.headers["location"])
        assert "error=" not in location, location
        return interface_id

    def page(org, *, stage="department", descriptor=None, author=None):
        """造一张待审页——但走的是真实入口：新建 → 提交审核。

        `stage="company"` 不是「把 review_stage 写成 company」，而是真的让部门初审过一票：
        只有公开页/公司汇总页会被推上去，所以这一档的描述符必须是公开页。
        """
        who = author or author_for(org)
        wanted = json.loads(json.dumps(descriptor or DESCRIPTOR))
        if stage == "company":
            wanted.setdefault("meta", {})["visibility"] = "public"
        interface_id = create(org, descriptor=wanted, author=who)
        submit(org, interface_id)
        if stage == "company":
            as_(DESIGN_REVIEWER)
            pushed = _approve(room, interface_id, note="部门初审过")
            assert pushed.headers["location"] == "/reviews?saved=passed", pushed.headers["location"]
            assert _row(room, interface_id)["review_stage"] == "company"
        as_(who)
        return interface_id

    room = {"conn": conn, "client": client, "as_": as_, "token": _token(state["cookie"]),
            "page": page, "create": create, "submit": submit,
            "design": design, "hr": hr, "people": people}
    try:
        yield room
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
             FROM oams.interfaces WHERE interface_id = %s""",
        (interface_id,)).fetchone()


def _stamp(room, interface_id):
    """描述符里那枚批准章：谁签的、哪一级、第几版。"""
    return room["conn"].execute(
        "SELECT descriptor FROM oams.interfaces WHERE interface_id = %s",
        (interface_id,)).fetchone()["descriptor"]["meta"]["review"]


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
    # 标题不带「初审」：这张表里本来就混着公司终审的页，说成只等初审会让人看漏。
    assert "别处还在等" in body
    assert "别处还在等初审" not in body
    # 在等谁一列按 review_stage 说实话，这页是部门初审。
    assert "部门初审" in body


def test_a_department_with_an_empty_queue_says_so_plainly(room):
    # 自己交的那版不给自己审，队列空着；他没有公司终审权，也不该提全公司。
    own = room["page"](room["design"], author=DESIGN_REVIEWER)

    room["as_"](DESIGN_REVIEWER)
    body = room["client"]().get("/reviews").text

    assert f"/reviews/{own}/approve" not in body
    assert "没有轮到你审的页面。" in body
    assert "全公司还在等" not in body


def test_the_platform_with_nothing_to_sign_points_at_the_queue_below(room):
    # 只有部门初审在等：平台没有卡可签，但下面那张表里有东西，空态得说实话。
    waiting = room["page"](room["design"])

    room["as_"](PLATFORM)
    body = room["client"]().get("/reviews").text

    assert f"/reviews/{waiting}/approve" not in body
    assert "没有等你签的页面，全公司还在等的列在下面。" in body


def test_the_platform_says_the_departments_are_done_when_nothing_is_waiting(room):
    room["as_"](PLATFORM)
    body = room["client"]().get("/reviews").text

    assert "部门都签完了，没有等你终审的页面。" in body
    assert "别处还在等" not in body


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
        """SELECT after_state FROM oams.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.review_stage_passed'""",
        (page,)).fetchall()
    assert len(audit) == 1
    # 这一趟签的是部门那一级
    assert audit[0]["after_state"]["stage"] == "department"

    room["as_"](PLATFORM)
    _approve(room, page, note="公司过")
    row = _row(room, page)
    assert (row["status"], row["visibility"]) == ("active", "public")
    final = room["conn"].execute(
        """SELECT after_state FROM oams.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.approved'
            ORDER BY occurred_at DESC LIMIT 1""",
        (page,)).fetchone()["after_state"]
    assert (final["stage"], final["acting"]) == ("company", "company")


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
    event = room["conn"].execute(
        """SELECT after_state, actor_member_id FROM oams.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.approved'
            ORDER BY occurred_at DESC LIMIT 1""",
        (page,)).fetchone()
    payload = event["after_state"]
    # 记录不能假装部门自己过的：签的是部门那一级，出手的是平台这个人。
    assert payload["acting"] == "company_proxy"
    assert payload["stage"] == "department"
    platform_id = room["conn"].execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (PLATFORM,)).fetchone()["member_id"]
    assert event["actor_member_id"] == platform_id


def test_a_public_page_is_signed_by_two_people_from_end_to_end(room):
    """两级审核从头到尾走界面，一步 SQL 都不用：建页 → 提交 → 部门初审 → 公司终审 → 启用。

    这条路以前走不通：公开页在起草时就被要求「已经批准」，而能写这个字段的只有作者，
    于是「等公司终审的公开页」在真实流程里根本造不出来。现在批准是启用那一票盖的章，
    所以这条链子必须能一路走到底，而且盖的是谁的章、哪一级，库里读得回来。
    """
    # 1. 设计师建页，声明要对外公开
    room["as_"](DESIGN_AUTHOR)
    page = room["create"](room["design"], descriptor=_page(visibility="public"))
    assert _row(room, page)["status"] == "draft"

    # 2. 他提交审核：draft → pending（部门初审）
    room["submit"](room["design"], page)
    row = _row(room, page)
    assert (row["status"], row["review_stage"]) == ("pending", "department")
    assert row["reviewed_at"] is None

    # 3. 部门负责人过第一关：不给章，只把页子推上去
    room["as_"](DESIGN_REVIEWER)
    first = _approve(room, page, note="部门初审过")
    assert first.headers["location"] == "/reviews?saved=passed"
    row = _row(room, page)
    assert (row["status"], row["review_stage"]) == ("pending", "company")
    assert row["reviewed_at"] is None, "部门这一票不是最终决定，不该留下 reviewed_*"
    # 作者自己写的 draft 没有被当成批准：章要等启用那一票才盖
    assert _stamp(room, page)["status"] == "draft"

    # 4. 平台终审：这一票才盖章启用
    room["as_"](PLATFORM)
    final = _approve(room, page, note="公司终审过")
    assert final.headers["location"] == "/reviews?saved=approved"

    row = _row(room, page)
    assert (row["status"], row["visibility"]) == ("active", "public")
    assert row["reviewed_by"] == room["people"][PLATFORM]
    assert row["reviewed_by"] != room["people"][DESIGN_AUTHOR], "审批人不能是作者自己"

    stamp = _stamp(room, page)
    assert stamp["status"] == "approved"
    assert stamp["reviewed_by"] == PLATFORM, "章上写的是审批人的工号，不是作者"
    assert stamp["stage"] == "company", "最终签字属于公司那一级"
    assert stamp["version_no"] == 1
    assert stamp["reviewed_at"], "签名没有时间就没法追责"

    # 5. 启用之后页面真的能打开
    opened = room["client"]().get(f"/interfaces/{page}")
    assert opened.status_code == 200
    assert "审核分级样张" in opened.text

    # 6. 审计里两级各留一条，级别各自对得上
    events = room["conn"].execute(
        """SELECT event_type, after_state FROM oams.workforce_audit_events
            WHERE subject_id = %s ORDER BY occurred_at""", (page,)).fetchall()
    by_type = {event["event_type"]: event["after_state"] for event in events}
    assert by_type["interface.review_stage_passed"]["stage"] == "department"
    assert by_type["interface.approved"]["stage"] == "company"
    assert by_type["interface.approved"]["acting"] == "company"
