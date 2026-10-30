"""`/setup/interfaces` 是唯一一条不经过审核就能上线的门，它得自己把章验到底。

上一轮堵住的是「作者在 JSON 里写 approved」；这一轮堵剩下的半个：章上那个名字。
`reviewed_by: 王二` 形状上完全合规，只有连库才知道公司里有没有这个人。所以这里不打
单元测试的主意，直接从真实路由点进去看它拒不拒——`/setup` 的手写章被拒，是这个洞唯
一能被看见的地方。

一笔事务里跑完，结束回滚，种子库不留痕。
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

import workhub.api
from workhub import web
from workhub.config import settings

pytestmark = pytest.mark.db

ADMIN = "ADMIN001"          # 系统管理员：有 workforce.work_items/manage，能走 /setup


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
    return {"conn": conn, "client": client(), "org": org}


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
    response = _submit(desk, _descriptor(ADMIN))
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert "error=" not in location, urllib.parse.unquote(location)
    assert location.endswith("saved=1"), location
    row = desk["conn"].execute(
        """SELECT status, reviewed_by FROM workhub.interfaces
            WHERE org_unit_id = %s AND name = '签字人核对'
            ORDER BY version_no DESC LIMIT 1""", (desk["org"],)).fetchone()
    assert row is not None and row["status"] == "active"
