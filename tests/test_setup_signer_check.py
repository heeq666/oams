"""`/setup/interfaces` 是唯一一条不经过审核就能上线的门，它得自己把章验到底。

上一轮堵住的是「作者在 JSON 里写 approved」；这一轮堵剩下的半个：章上那个名字。
`reviewed_by: 王二` 形状上完全合规，只有连库才知道公司里有没有这个人。所以这里不打
单元测试的主意，直接从真实路由点进去看它拒不拒——`/setup` 的手写章被拒，是这个洞唯
一能被看见的地方。

一笔事务里跑完，结束回滚，种子库不留痕。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import json
import urllib.parse
import uuid

import pytest
from starlette.testclient import TestClient

import workhub.api
from workhub import web
from workhub.config import settings

pytestmark = pytest.mark.db

ADMIN = "ADMIN001"          # 系统管理员：有 workforce.work_items/manage，能走 /setup
SIGNER = "DESIGN001"        # 设计部负责人：这一页的签字人，不是点按钮的人


def _token(cookie: str) -> str:
    return hmac.new(cookie.encode(), b"workhub:interface-setup-form:v1",
                    hashlib.sha256).hexdigest()


def _descriptor(reviewer: str, org_code: str = "DESIGN") -> dict:
    """能过校验的一页公开界面，章是手写的。"""
    return {
        "schema_version": "0.2",
        "interface_id": "signer-check",
        "name": "签字人核对",
        "org_unit_code": org_code,
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
                 "visibility": "public",
                 "review": {"status": "approved", "reviewed_by": reviewer,
                            "reviewed_at": "2026-10-31T09:00:00Z"}},
    }


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


@pytest.fixture()
def desk(seeded_database, monkeypatch):
    """种子库 + 一个以 ADMIN001 身份说话的 TestClient，整笔回滚。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(workhub.api, "connection", lambda: _one_transaction(conn))

    me = conn.execute(
        "SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s",
        (ADMIN,)).fetchone()["member_id"]
    monkeypatch.setattr(workhub.api, "principal_from_request",
                        lambda request: {"member_id": me, "employee_code": ADMIN,
                                         "display_name": ADMIN,
                                         "employment_status": "active"})

    def client() -> TestClient:
        made = TestClient(workhub.api.app)
        made.cookies.set(settings.web_session_cookie, "setup-signer-test")
        return made

    org = seeded_database.org_unit_id("DESIGN")
    try:
        yield {"conn": conn, "client": client(), "org": org, "me": me}
    finally:
        conn.rollback()
        conn.close()


def _submit(desk, descriptor: dict):
    return desk["client"].post(
        "/setup/interfaces",
        data={"csrf": _token("setup-signer-test"), "org_unit_id": str(desk["org"]),
              "slug": f"signer-{uuid.uuid4().hex[:8]}", "name": "签字人核对",
              "position_id": "", "descriptor_text": json.dumps(descriptor)},
        follow_redirects=False)


def test_setup_refuses_a_stamp_signed_by_somebody_who_does_not_work_here(desk):
    response = _submit(desk, _descriptor("王二"))
    assert response.status_code == 303, response.text
    location = urllib.parse.unquote(response.headers["location"])
    assert "error=" in location, location
    assert "must name an active member" in location
    assert "王二" in location


def test_setup_accepts_a_stamp_signed_by_a_real_employee(desk):
    """章上的人和点按钮的人**不是同一个**，行上要记章上那个。

    这个场景就是 /setup 存在的理由：管理员替别的部门把一页已经签好的界面导进来。
    所以签字人得挑一个不是当前登录者的人 —— 否则「拿 principal 的 member_id 顶上」
    这种偷懒写法也会绿，正是测试上一轮审出来的那条。
    """
    signer = desk["conn"].execute(
        "SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s",
        (SIGNER,)).fetchone()["member_id"]
    assert signer != desk["me"], "签字人不能就是点按钮的人，否则这条测不出东西"

    response = _submit(desk, _descriptor(SIGNER))
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert "error=" not in location, urllib.parse.unquote(location)
    assert location.endswith("saved=1"), location
    row = desk["conn"].execute(
        """SELECT status, reviewed_by, reviewed_at, descriptor->'meta'->'review' AS review
             FROM workhub.interfaces
            WHERE org_unit_id = %s AND name = '签字人核对'
            ORDER BY version_no DESC LIMIT 1""", (desk["org"],)).fetchone()
    assert row is not None and row["status"] == "active"
    # 章上写了谁，行上就得记着谁 —— 光有 active 证明不了这条路把签字人带过去了。
    assert row["reviewed_by"] == signer, row
    # 时间也来自文件那一个章，不是落库那一刻的 now()。
    assert row["reviewed_at"] is not None, row
    assert row["reviewed_at"].astimezone(datetime.timezone.utc) == datetime.datetime(
        2026, 10, 31, 9, 0, tzinfo=datetime.timezone.utc), row
    assert row["review"]["reviewed_by"] == SIGNER, row

def test_a_signature_from_somebody_who_already_left_is_refused(desk):
    """「在职」两个字得真的被看见。

    `reviewed_by` 写的是一个确有其人、只是已经离职的工号：名单要是没按
    `employment_status='active'` 筛，他签的章就会和在职的人一样能上线，而这正是
    交接、离职、换岗之后最容易被借用的那张脸。
    """
    desk["conn"].execute(
        """INSERT INTO workhub.workforce_members
                 (employee_code, display_name, employment_status, joined_at, departed_at)
           VALUES ('LEFT001', '已离职的老王', 'departed', current_date - 400, current_date - 1)""")
    response = _submit(desk, _descriptor("LEFT001"))
    assert response.status_code == 303, response.text
    location = urllib.parse.unquote(response.headers["location"])
    assert "must name an active member" in location, location
    assert "LEFT001" in location
    assert desk["conn"].execute(
        "SELECT 1 FROM workhub.interfaces WHERE name = '签字人核对'").fetchone() is None, \
        "被拒的那一页不该留下半条记录"
