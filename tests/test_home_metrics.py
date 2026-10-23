"""Company metrics in the web layer (task-6).

The numbers come from workhub.funnel over a FixtureDataCenter, so these tests
carry the contract's measured 26D冬1-2波 values (59 / 9 / 29 / 39 /
¥1,745.87 万 / 5.18) and need neither a data centre nor a database.

The database is stubbed at the module boundary (workhub.web.connection and the
security helpers web.py imported), because everything under test here is the
presentation of platform numbers, not persistence. The one behaviour that must
never regress is that an unreachable data centre degrades the panel instead of
answering 500 -- that is why each failure mode appears twice, once against the
helper and once through a real HTTP request.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

# api imports workhub.web at the bottom of the module, so it must be imported
# first: the other order is a circular import.
from workhub.api import app
from workhub import metrics, web
from workhub.config import settings
from workhub.datacenter import DataCenterError, DataCenterNotConfigured, FixtureDataCenter

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "datacenter_26d.json"
WAVE = "26D冬1-2波"


def fixture_dc() -> FixtureDataCenter:
    return FixtureDataCenter(FIXTURE)


# --------------------------------------------------------------------------- #
# A database stand-in: only the few queries the pages actually need
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Answers the handful of SELECTs the board and department pages issue."""

    def __init__(self, org_unit: dict) -> None:
        self.org_unit = org_unit

    def execute(self, sql: str, params: Any = None) -> _Result:
        text = " ".join(sql.split())
        if "FROM workhub.interfaces" in text:
            return _Result([])
        if "FROM workhub.org_units" in text:
            return _Result([self.org_unit])
        return _Result([])


@contextmanager
def _fake_connection(conn: _FakeConn):
    yield conn


ADMIN = {"member_id": uuid4(), "display_name": "测试管理员", "session_id": uuid4()}


@pytest.fixture()
def org_unit() -> dict:
    return {"org_unit_id": uuid4(), "org_code": "DESIGN", "name": "设计部", "unit_type": "department"}


@pytest.fixture()
def stubbed(monkeypatch, org_unit):
    """No data centre URL, no database, no session table -- just the pages."""
    conn = _FakeConn(org_unit)

    # A fresh context manager per call: 'connection' is re-entered by several
    # helpers in one request, and one spent generator cannot be entered twice.
    def connection_stub():
        return _fake_connection(conn)

    app_module = __import__("workhub.api", fromlist=["api"])
    for module in (web, app_module):
        monkeypatch.setattr(module, "connection", connection_stub)
    monkeypatch.setattr(web, "has_permission", lambda *a, **k: True)
    monkeypatch.setattr(web, "has_company_scope", lambda *a, **k: True)
    monkeypatch.setattr(web, "require_org_unit_access", lambda *a, **k: None)
    monkeypatch.setattr(web, "active_org_unit_ids", lambda *a, **k: {org_unit["org_unit_id"]})
    monkeypatch.setattr(app_module, "has_permission", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "has_company_scope", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "active_org_unit_ids", lambda *a, **k: {org_unit["org_unit_id"]})
    monkeypatch.setattr(app_module, "principal_from_request", lambda request: ADMIN)
    monkeypatch.setattr(web, "build_datacenter", lambda dc=None: fixture_dc())
    monkeypatch.setattr(settings, "default_wave", None)
    return conn


BOARD = {
    "org_unit": {"org_unit_id": uuid4(), "name": "设计部"},
    "generated_at": None,
    "stale_days": 7,
    "layer": "all",
    "stage": None,
    "summary": {
        "open_items": 12, "blocked": 1, "needs_decision": 2, "never_reported": 3,
        "overdue": 4, "positions_without_holder": 0,
        "latest_report_by_channel": {"human": 1, "agent": 2},
    },
    "layers": [{"id": "all", "label": "全部", "hint": "所有图层", "count": 12}],
    "flow": [],
    "visible_cards": [],
    "cards": [],
    "sections": {},
    "child_org_units": [],
}


# --------------------------------------------------------------------------- #
# The panel helper
# --------------------------------------------------------------------------- #

def test_panel_is_available_with_the_fixture(stubbed):
    panel = web.company_metrics_panel()

    assert panel["available"] is True
    assert panel["reason"] is None
    # Wave comes from the data centre when nothing is configured.
    assert panel["wave"] == WAVE
    assert tuple(row["key"] for row in panel["metrics"]) == metrics.HOME_DEFAULT


def test_panel_labels_and_units_come_from_the_catalogue(stubbed):
    rows = {row["key"]: row for row in web.company_metrics_panel()["metrics"]}

    assert rows["funnel.sample_done"]["label"] == "已出样"
    assert rows["funnel.sample_open"]["label"] == "打样未完成"
    assert rows["funnel.ordered"]["label"] == "已下单"
    assert rows["funnel.not_ordered"]["label"] == "未下单"
    assert rows["funnel.gmv"]["label"] == "下单金额"
    assert rows["funnel.roi"]["label"] == "推广 ROI"
    assert [rows[key]["unit"] for key in metrics.HOME_DEFAULT] == ["款", "款", "款", "款", "元", "倍"]


def test_panel_reproduces_the_contract_numbers(stubbed):
    rows = {row["key"]: row for row in web.company_metrics_panel()["metrics"]}

    assert rows["funnel.sample_done"]["value"] == 59
    assert rows["funnel.sample_open"]["value"] == 9
    assert rows["funnel.ordered"]["value"] == 29
    assert rows["funnel.not_ordered"]["value"] == 39
    assert rows["funnel.gmv"]["value"] == 17458700
    assert rows["funnel.roi"]["value"] == pytest.approx(5.1751, abs=1e-4)


def test_panel_uses_the_single_formatter(stubbed):
    rows = {row["key"]: row for row in web.company_metrics_panel()["metrics"]}

    assert rows["funnel.sample_done"]["formatted"] == "59 款"
    assert rows["funnel.sample_open"]["formatted"] == "9 款"
    assert rows["funnel.ordered"]["formatted"] == "29 款"
    assert rows["funnel.not_ordered"]["formatted"] == "39 款"
    assert rows["funnel.gmv"]["formatted"] == "¥1,745.87 万"
    # Rounded half up by owner ruling (2026-10-23): 5.1751 -> 5.18.
    assert rows["funnel.roi"]["formatted"] == "5.18"


def test_configured_wave_wins_over_the_first_wave(stubbed, monkeypatch):
    monkeypatch.setattr(settings, "default_wave", "26D冬3-4波")

    panel = web.company_metrics_panel()

    assert panel["wave"] == "26D冬3-4波"
    rows = {row["key"]: row for row in panel["metrics"]}
    assert rows["funnel.sample_done"]["value"] == 1
    assert rows["funnel.ordered"]["value"] == 1


class _TimeoutCentre:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout


def test_page_wait_for_the_data_centre_is_capped(monkeypatch):
    # A page must not inherit the client's 20s batch default: behind a
    # firewalled LAN host the board would sit there before saying "暂不可用".
    centre = _TimeoutCentre()
    monkeypatch.setattr(web, "build_datacenter", lambda dc=None: centre)

    assert web._page_datacenter() is centre
    assert centre.timeout <= web.METRICS_HTTP_TIMEOUT
    assert web.METRICS_HTTP_TIMEOUT < 20.0


def test_an_explicitly_passed_data_centre_keeps_its_own_timeout():
    # Tests and the CLI pass a prepared client; the page must not re-tune it.
    dc = _TimeoutCentre(99.0)

    assert web._page_datacenter(dc) is dc
    assert dc.timeout == 99.0


def test_unconfigured_data_centre_degrades_instead_of_raising(stubbed, monkeypatch):
    def boom(dc=None):
        raise DataCenterNotConfigured("商品数据中心地址未配置")

    monkeypatch.setattr(web, "build_datacenter", boom)

    panel = web.company_metrics_panel()

    assert panel["available"] is False
    assert panel["metrics"] == []
    assert "未配置" in panel["reason"]
    assert "WORKHUB_DATACENTER_URL" in panel["reason"]


def test_data_centre_error_degrades_instead_of_raising(stubbed, monkeypatch):
    def boom(dc=None):
        raise DataCenterError("连接超时")

    monkeypatch.setattr(web, "build_datacenter", boom)

    panel = web.company_metrics_panel()

    assert panel["available"] is False
    assert panel["reason"] == "连接超时"


def test_any_unexpected_error_degrades_too(stubbed, monkeypatch):
    # The data centre is somebody else's machine: a json decode error, a missing
    # key or a TypeError in a payload must be just as survivable as a timeout.
    def boom(dc=None):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(web, "build_datacenter", boom)

    panel = web.company_metrics_panel()

    assert panel["available"] is False
    assert "NoneType" in panel["reason"]


# --------------------------------------------------------------------------- #
# The department funnel chain
# --------------------------------------------------------------------------- #

def test_department_chain_has_the_five_stages(stubbed):
    chain = web.department_funnel_chain()

    assert chain["available"] is True
    assert chain["wave"] == WAVE
    assert [stage["label"] for stage in chain["stages"]] == [
        "有开发记录", "已出样", "有核价记录", "核价完成", "已下单"]


def test_department_chain_counts_come_from_the_same_payload(stubbed):
    counts = {stage["label"]: stage["count"] for stage in web.department_funnel_chain()["stages"]}

    # 68 styles in the wave, 66 with a development record, 46 of those carrying a
    # pricing status, 13 of which are 核价完成.
    assert counts["有开发记录"] == 66
    assert counts["已出样"] == 59
    assert counts["有核价记录"] == 46
    assert counts["核价完成"] == 13
    assert counts["已下单"] == 29


def test_department_chain_stages_never_exceed_the_style_count(stubbed):
    chain = web.department_funnel_chain()
    counts = {stage["label"]: stage["count"] for stage in chain["stages"]}

    # The first four stages are nested -- a style cannot be 已出样 without a
    # development record, nor 核价完成 without a pricing record -- so they must
    # shrink. 已下单 is deliberately *not* nested: a shop can order a style whose
    # sampling record was never filled in, which is exactly why 29 > 13.
    assert counts["有开发记录"] >= counts["已出样"]
    assert counts["有开发记录"] >= counts["有核价记录"] >= counts["核价完成"]
    assert counts["已出样"] <= 68
    assert counts["已下单"] == 29


def test_department_chain_degrades(stubbed, monkeypatch):
    def boom(dc=None):
        raise DataCenterError("DNS 解析失败")

    monkeypatch.setattr(web, "build_datacenter", boom)

    chain = web.department_funnel_chain()

    assert chain["available"] is False
    assert chain["stages"] == []
    assert chain["reason"] == "DNS 解析失败"


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def test_home_json_returns_the_six_metrics(stubbed):
    response = TestClient(app).get("/metrics/home.json")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["wave"] == WAVE
    assert [row["key"] for row in body["metrics"]] == list(metrics.HOME_DEFAULT)
    for row in body["metrics"]:
        assert set(row) == {"key", "label", "value", "formatted", "unit"}
    assert body["metrics"][0]["formatted"] == "59 款"
    assert body["metrics"][4]["formatted"] == "¥1,745.87 万"
    assert body["metrics"][5]["formatted"] == "5.18"


def test_home_json_is_json_not_html(stubbed):
    response = TestClient(app).get("/metrics/home.json")

    assert response.headers["content-type"].startswith("application/json")
    assert json.loads(response.text)["available"] is True


def test_home_json_never_500s_when_the_data_centre_is_down(stubbed, monkeypatch):
    def boom(dc=None):
        raise DataCenterNotConfigured("商品数据中心地址未配置")

    monkeypatch.setattr(web, "build_datacenter", boom)

    response = TestClient(app).get("/metrics/home.json")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["metrics"] == []
    assert "未配置" in body["reason"]


def test_home_json_requires_a_session(monkeypatch):
    api_module = __import__("workhub.api", fromlist=["api"])
    monkeypatch.setattr(api_module, "principal_from_request", lambda request: None)

    response = TestClient(app).get("/metrics/home.json")

    assert response.status_code == 401
    assert response.json()["available"] is False


def test_home_json_refuses_without_the_permission(stubbed, monkeypatch):
    monkeypatch.setattr(web, "has_permission", lambda *a, **k: False)

    response = TestClient(app).get("/metrics/home.json")

    assert response.status_code == 403
    assert "workforce.work_items/read" in response.json()["reason"]


# --------------------------------------------------------------------------- #
# The pages
# --------------------------------------------------------------------------- #

def test_board_renders_the_company_panel(stubbed, monkeypatch):
    monkeypatch.setattr(web.api, "build_board", lambda *a, **k: BOARD)

    response = TestClient(app).get("/board")

    assert response.status_code == 200
    html = response.text
    assert "公司指标" in html
    assert WAVE in html
    assert "59 款" in html
    assert "¥1,745.87 万" in html
    assert "5.18" in html
    # The department cards must survive underneath it.
    assert "未完成工作项" in html


def test_board_survives_an_unreachable_data_centre(stubbed, monkeypatch):
    monkeypatch.setattr(web.api, "build_board", lambda *a, **k: BOARD)

    def boom(dc=None):
        raise DataCenterNotConfigured("商品数据中心地址未配置")

    monkeypatch.setattr(web, "build_datacenter", boom)

    response = TestClient(app).get("/board")

    assert response.status_code == 200
    html = response.text
    assert "指标暂不可用" in html
    # The rest of the board is still there: a dead LAN cable must not blank a page.
    assert "未完成工作项" in html
    assert "部门进度看板" in html
    assert "12" in html


def test_board_body_fragment_also_carries_the_panel(stubbed, monkeypatch):
    # The htmx swap replaces the whole fragment; a panel rendered only on the full
    # page would vanish the first time somebody switches department.
    monkeypatch.setattr(web.api, "build_board", lambda *a, **k: BOARD)
    org = uuid4()

    response = TestClient(app).get(f"/board/body?org={org}")

    assert response.status_code == 200
    assert "公司指标" in response.text
    assert "59 款" in response.text


def test_department_page_shows_the_funnel_chain(stubbed):
    org = stubbed.org_unit["org_unit_id"]

    response = TestClient(app).get(f"/departments/{org}")

    assert response.status_code == 200
    html = response.text
    assert "公司产品漏斗" in html
    assert "有开发记录" in html
    assert "核价完成" in html
    assert "已下单" in html


def test_department_page_survives_an_unreachable_data_centre(stubbed, monkeypatch):
    def boom(dc=None):
        raise DataCenterError("连接被拒绝")

    monkeypatch.setattr(web, "build_datacenter", boom)
    org = stubbed.org_unit["org_unit_id"]

    response = TestClient(app).get(f"/departments/{org}")

    assert response.status_code == 200
    assert "公司产品漏斗暂不可用" in response.text
    assert "连接被拒绝" in response.text
    # The department's own empty state must still render.
    assert "新建页面" in response.text
