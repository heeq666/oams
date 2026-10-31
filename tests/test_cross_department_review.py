"""跨部门底座：从别的部门那一页起手，审到公司终审。

`based_on` 原来只会指向**本部门同 slug 的上一版**，于是「跨部门」这条判据在真实使用
里恒假：公司终审那半条升级路径一次都没被走到过。这一组用例从真实入口把路走通——

1. 设计部的人在自己的新建页上，能看见别的部门的公开页面，并把它选作基础；
2. 选出来之后 `based_on` 真的落在库里，指向那一页；
3. 提交审核时仍是部门初审，部门过了**不上线**，`review_stage` 翻成 company；
4. 平台终审过完才 active。

顺带钉住一条看不见的边界：看不见的页面不能被选作基础，报的话只能是「不存在或不开放给
你」——两句话分开说，等于告诉人「这页存在，只是你不够格」。

一笔事务里跑完，结束回滚。
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
from oams import web
from oams.config import settings

pytestmark = pytest.mark.db

DESIGN_REVIEWER = "DESIGN001"   # 设计部负责人：能审本部门初审，没有 review_company
DESIGN_AUTHOR = "DESIGN002"     # 设计部设计师：能建页、能提交（本文件事务里现造）
PLATFORM = "ADMIN001"           # 系统管理员：公司终审
HR_AUTHOR = "MVP1001"           # 人事经理：那一页的作者，用来造「别的部门的页」

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "cross-department-sample",
    "name": "跨部门样张",
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


def _page_token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"workhub:page-form:v1", hashlib.sha256).hexdigest()


def _review_token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"workhub:review-form:v1", hashlib.sha256).hexdigest()


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


@pytest.fixture()
def room(seeded_database, monkeypatch):
    """种子库 + 一笔事务 + 能按身份切人的 TestClient。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    # 设计部得有个「不是审核人」的作者：种子里这个部门只有负责人，而他就是审核人。
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
    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (DESIGN_REVIEWER, PLATFORM, DESIGN_AUTHOR, HR_AUTHOR)}

    # 人事部有一张已经上线的公开页：这就是设计部可以「照着做」的那一页。
    # 公开页在库里要求 reviewed_by/reviewed_at 非空（表自身的约束），所以照填。
    source = conn.execute(
        """INSERT INTO oams.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                version_no, created_by, updated_by, reviewed_by, reviewed_at)
           VALUES (%s, 'hr-public-sample', '人事部招聘流程', 'active', 'public', '0.2', %s, 1, %s, %s, %s, now())
           RETURNING interface_id""",
        (hr, json.dumps({**DESCRIPTOR, "org_unit_code": "HR", "name": "人事部招聘流程"}),
         people[HR_AUTHOR], people[HR_AUTHOR], people[PLATFORM])).fetchone()["interface_id"]
    # 再加一张只有人事部自己能看见的页：它不许出现在设计部的选择里。
    hidden = conn.execute(
        """INSERT INTO oams.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                version_no, created_by, updated_by)
           VALUES (%s, 'hr-private-sample', '人事部内部名单', 'active', 'department', '0.2', %s, 1, %s, %s)
           RETURNING interface_id""",
        (hr, json.dumps({**DESCRIPTOR, "org_unit_code": "HR", "name": "人事部内部名单"}),
         people[HR_AUTHOR], people[HR_AUTHOR])).fetchone()["interface_id"]

    state = {"me": DESIGN_AUTHOR, "cookie": "cross-department-test"}
    monkeypatch.setattr(oams.api, "principal_from_request",
                        lambda request: {"member_id": people[state["me"]],
                                         "employee_code": state["me"],
                                         "display_name": state["me"],
                                         "employment_status": "active"})
    client = TestClient(oams.api.app)
    client.cookies.set(settings.web_session_cookie, state["cookie"])

    def as_(code):
        state["me"] = code

    try:
        yield {"conn": conn, "client": client, "as_": as_,
               "design": design, "source": source, "hidden": hidden}
    finally:
        # 一条事务要有人收尾：不回滚就关，下一条用例的同名插入会等在这条
        # 事务的锁上，直到超时（第一次跑就是这么挂住的）。
        conn.rollback()
        conn.close()


def test_the_new_page_form_offers_the_other_departments_public_page(room):
    response = room["client"].get(f"/departments/{room['design']}/new")
    assert response.status_code == 200, response.text
    assert "based_on" in response.text, "新建页上要能选基础页"
    assert "人事部招聘流程" in response.text
    assert "人事部内部名单" not in response.text, "看不见的页面不许列出来"


def test_a_page_built_on_another_departments_page_goes_to_the_company(room):
    client = room["client"]
    created = client.post(
        f"/departments/{room['design']}/new",
        data={"csrf": _page_token("cross-department-test"), "name": "设计部借用人事流程",
              "slug": f"borrow-{uuid.uuid4().hex[:6]}", "position_id": "",
              "descriptor_text": json.dumps(DESCRIPTOR), "based_on": str(room["source"])},
        follow_redirects=False)
    assert created.status_code == 303, created.text
    location = created.headers["location"]
    assert "error=" not in location, urllib.parse.unquote(location)
    interface_id = location.split("/")[-1].split("?")[0]
    row = room["conn"].execute(
        """SELECT based_on, status, review_stage FROM oams.interfaces
            WHERE interface_id = %s""", (interface_id,)).fetchone()
    assert str(row["based_on"]) == str(room["source"]), "基础页要真的记在这一版上"
    assert row["status"] == "draft" and row["review_stage"] == "department"

    submitted = client.post(
        f"/departments/{room['design']}/{interface_id}/submit-review",
        data={"review_csrf": _page_token("cross-department-test")}, follow_redirects=False)
    assert submitted.status_code == 303 and "saved=review" in submitted.headers["location"]

    # 部门初审过：跨部门的页子不上线，翻到公司终审那一级。
    room["as_"](DESIGN_REVIEWER)
    passed = client.post(f"/reviews/{interface_id}/approve",
                         data={"csrf": _review_token("cross-department-test"), "note": ""},
                         follow_redirects=False)
    assert "saved=passed" in passed.headers["location"], passed.headers["location"]
    row = room["conn"].execute(
        """SELECT status, review_stage FROM oams.interfaces WHERE interface_id = %s""",
        (interface_id,)).fetchone()
    assert row["status"] == "pending", "部门过了不等于上线"
    assert row["review_stage"] == "company", "跨部门的页要升到公司终审"

    # 平台终审过：这才 active。
    room["as_"](PLATFORM)
    final = client.post(f"/reviews/{interface_id}/approve",
                        data={"csrf": _review_token("cross-department-test"), "note": ""},
                        follow_redirects=False)
    assert "saved=approved" in final.headers["location"], final.headers["location"]
    row = room["conn"].execute(
        """SELECT status, reviewed_by, descriptor->'meta'->'review'->>'stage' AS stage
             FROM oams.interfaces WHERE interface_id = %s""", (interface_id,)).fetchone()
    assert row["status"] == "active"
    assert row["stage"] == "company", "章上写的是公司终审这一级"


def test_a_page_you_cannot_see_cannot_be_chosen_as_a_basis(room):
    response = room["client"].post(
        f"/departments/{room['design']}/new",
        data={"csrf": _page_token("cross-department-test"), "name": "偷看人事名单",
              "slug": f"peek-{uuid.uuid4().hex[:6]}", "position_id": "",
              "descriptor_text": json.dumps(DESCRIPTOR), "based_on": str(room["hidden"])},
        follow_redirects=False)
    assert response.status_code == 303
    message = urllib.parse.unquote(response.headers["location"])
    assert "不存在，或者不开放给你" in message, message
    # 同一句话同时盖住「没有这一页」和「你不能看」：分开说就等于确认它存在。
    assert "不开放" in message


def test_choosing_your_own_departments_previous_version_is_pointless_and_says_so(room):
    """本部门上一版本来就会自动接上；在这里再选一次只会多一条错误来源。"""
    room["conn"].execute(
        """INSERT INTO oams.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                version_no, created_by, updated_by)
           VALUES (%s, 'design-own-sample', '设计部自家页', 'active', 'department', '0.2', %s, 1, %s, %s)""",
        (room["design"], json.dumps(DESCRIPTOR),
         room["conn"].execute(
             "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
             (DESIGN_AUTHOR,)).fetchone()["member_id"],
         room["conn"].execute(
             "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
             (DESIGN_AUTHOR,)).fetchone()["member_id"]))
    own = room["conn"].execute(
        "SELECT interface_id FROM oams.interfaces WHERE slug = 'design-own-sample'").fetchone()
    response = room["client"].post(
        f"/departments/{room['design']}/new",
        data={"csrf": _page_token("cross-department-test"), "name": "多余指定",
              "slug": f"redundant-{uuid.uuid4().hex[:6]}", "position_id": "",
              "descriptor_text": json.dumps(DESCRIPTOR), "based_on": str(own["interface_id"])},
        follow_redirects=False)
    assert response.status_code == 303
    assert "自动作为基础" in urllib.parse.unquote(response.headers["location"])
