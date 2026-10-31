"""启用那一票，行上、章上、审计里三处得说同一句话。

`review_stage` 列的含义是「这一页现在等着谁签」。页面一 active 就不再等谁了，所以这一格
记录的是**最后签的那一级**：平台终审过的写 company，部门自己点过就直接上线的写
department。写死成任何一边都是错的——写 company 等于替没签过的公司级背书，写
department 又把平台那一票抹掉了，两边都和审计里 `acting` 打架。

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

DESIGN_REVIEWER = "DESIGN001"   # 设计部负责人：部门初审
PLATFORM = "ADMIN001"           # 系统管理员：公司终审
AUTHOR = "DESIGN002"            # 设计部设计师：这两页的作者（不是审核人，本文件里现造）

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "activation-stage-sample",
    "name": "启用阶段样张",
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


def _review_token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"oams:review-form:v1", hashlib.sha256).hexdigest()


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


@pytest.fixture()
def hall(seeded_database, monkeypatch):
    """种子库 + 一笔事务 + 能按身份切人的 TestClient。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    design = seeded_database.org_unit_id("DESIGN")
    # 种子里设计部只有负责人一个人，而他就是审核人；作者得是别人，不然作者回避
    # 会把这两页一起挡掉，测试就测不到「启用写了什么」。
    conn.execute(
        """INSERT INTO oams.positions (position_code, title, org_unit_id)
           SELECT 'DESIGNER', '设计师', org_unit_id FROM oams.org_units
            WHERE org_code = 'DESIGN'""")
    conn.execute(
        """INSERT INTO oams.workforce_members
                 (employee_code, display_name, employment_status, joined_at)
           VALUES (%s, '设计师小李', 'active', current_date - 30)""", (AUTHOR,))
    conn.execute(
        """INSERT INTO oams.position_assignments
                 (member_id, position_id, assignment_type, status, starts_at, reason)
           SELECT m.member_id, p.position_id, 'primary', 'active',
                  now() - interval '30 days', 'seed'
             FROM oams.workforce_members m, oams.positions p
            WHERE m.employee_code = %s AND p.position_code = 'DESIGNER'""", (AUTHOR,))
    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (DESIGN_REVIEWER, PLATFORM, AUTHOR)}

    def pending(slug: str, stage: str) -> str:
        return str(conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    version_no, review_stage, created_by, updated_by)
               VALUES (%s, %s, %s, 'pending', 'department', '0.2', %s, 1, %s, %s, %s)
               RETURNING interface_id""",
            (design, slug, f"阶段样张·{slug}", json.dumps(DESCRIPTOR), stage,
             people[AUTHOR], people[AUTHOR])).fetchone()["interface_id"])

    state = {"me": PLATFORM, "cookie": "activation-stage-test"}
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
        yield {"conn": conn, "client": client, "as_": as_, "pending": pending}
    finally:
        conn.rollback()
        conn.close()


def _approve(hall, interface_id: str):
    response = hall["client"].post(
        f"/reviews/{interface_id}/approve",
        data={"csrf": _review_token("activation-stage-test"), "note": ""},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    return urllib.parse.unquote(response.headers["location"])


def _row(hall, interface_id: str):
    return hall["conn"].execute(
        """SELECT status, review_stage,
                  descriptor->'meta'->'review'->>'stage' AS stamp_stage,
                  descriptor->'meta'->'review'->>'status' AS stamp_status
             FROM oams.interfaces WHERE interface_id = %s""", (interface_id,)).fetchone()


def _last_audit(hall, interface_id: str):
    return hall["conn"].execute(
        """SELECT after_state FROM oams.workforce_audit_events
            WHERE subject_id = %s AND event_type = 'interface.approved'
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1""", (interface_id,)).fetchone()


def test_a_company_signature_leaves_the_row_at_the_company_level(hall):
    """平台终审过的那一页：行、章、审计都说 company。"""
    page = hall["pending"]("stage-company-sample", "company")
    hall["as_"](PLATFORM)
    assert _approve(hall, page).endswith("saved=approved")

    row = _row(hall, page)
    assert row["status"] == "active", row
    assert row["review_stage"] == "company", "行上要写实际签的那一级"
    assert row["stamp_stage"] == "company", "章上也要说的是公司这一级"
    assert row["review_stage"] == row["stamp_stage"], "行和章不许各说一句"
    audit = _last_audit(hall, page)
    assert audit is not None, "启用要留一条审计"
    assert audit["after_state"]["stage"] == "company", audit


def test_a_department_signature_that_goes_live_stays_a_department_signature(hall):
    """部门点通过就直接上线的那一页：三处都说 department，不许冒充公司签过。"""
    page = hall["pending"]("stage-department-sample", "department")
    hall["as_"](DESIGN_REVIEWER)
    assert _approve(hall, page).endswith("saved=approved")

    row = _row(hall, page)
    assert row["status"] == "active", row
    assert row["review_stage"] == "department", "部门签的就写 department"
    assert row["stamp_stage"] == "department", "章上也一样"
    audit = _last_audit(hall, page)
    assert audit["after_state"]["stage"] == "department", audit
