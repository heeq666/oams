"""状态分布与动态流必须来自真实数据（接口 v0.2 的 status_chart / activity_feed）。

这两个模块以前只在 preview 里有东西：`status_counts()` 读 `status.<词>`、
`activity_items()` 读 `activity`，而真页面喂 aggregates 的
`api.build_department_aggregates` 从不写这两个键——预览里画着柱子，真页面上永远写着
「还没有状态数据」。这组用例钉住三件事：

* 四个标准态按**真实条目**计数，草稿与已撤回不算干过的活；
* 部门自己的词各算各的（等配件 / 等厂商都映到 blocked，但不能互相吞）；
* 动态流来自既有的审计表，只出真发生过的行，没有就空着。
"""

from __future__ import annotations

import contextlib
import json

import pytest

from oams import api

# ---------------------------------------------------------------------------
# 纯函数：不连库也能钉住「怎么数」
# ---------------------------------------------------------------------------


def test_status_tally_counts_the_four_standard_states():
    rows = [{"status": "done"}, {"status": "done"}, {"status": "blocked"}, {"status": None}]
    assert api._status_tally(rows) == {"done": 2, "blocked": 1}


def test_status_word_tally_prefers_the_word_the_entry_carried():
    descriptor = {
        "fields": [{"key": "state_word", "map_to": "status"}],
        "status_map": {"等配件": "blocked", "等厂商": "blocked", "已恢复": "done"},
    }
    rows = [{"status": "blocked", "extra": {"state_word": "等配件"}},
            {"status": "blocked", "extra": {"state_word": "等厂商"}},
            {"status": "done", "extra": {"state_word": "已恢复"}}]
    assert api._status_word_tally(descriptor, rows) == {"等配件": 1, "等厂商": 1, "已恢复": 1}


def test_status_word_tally_refuses_to_guess_when_two_words_share_a_state():
    """老条目没有词（口径 A 之前记的）：两个词都映 blocked 时不挑其中一个。"""
    descriptor = {
        "fields": [{"key": "state_word", "map_to": "status"}],
        "status_map": {"等配件": "blocked", "等厂商": "blocked"},
    }
    rows = [{"status": "blocked", "extra": {}}]
    assert api._status_word_tally(descriptor, rows) == {}


def test_status_word_tally_uses_the_only_word_when_mapping_is_one_to_one():
    descriptor = {
        "fields": [{"key": "state_word", "map_to": "status"}],
        "status_map": {"等配件": "blocked", "已恢复": "done"},
    }
    rows = [{"status": "done", "extra": {}}]
    assert api._status_word_tally(descriptor, rows) == {"已恢复": 1}


# ---------------------------------------------------------------------------
# 真库：整条链路（条目 -> aggregates -> 模板要的那两个键）
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.db

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "status-fixture",
    "name": "状态分布测试页",
    "org_unit_code": "STATUSD",
    "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
    "modules": [{"key": "distribution", "type": "status_chart",
                 "params": {"dimension": "status"}}],
    "fields": [
        {"key": "title", "label": "事项", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True},
        {"key": "state_word", "label": "处理状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True,
         "options": ["等配件", "等厂商", "已恢复"]},
        {"key": "evidence_ref", "label": "依据", "type": "text", "required": True,
         "map_to": "evidence", "aggregatable": True},
    ],
    "status_map": {"等配件": "blocked", "等厂商": "blocked", "已恢复": "done"},
    "rules": {"evidence_required": True},
    "metrics": [],
}


def _insert_member(conn, employee_code, display_name):
    """自造一个成员：用例不依赖某台机器上 bootstrap-admin 起的那个管理员叫什么。"""
    return conn.execute(
        "INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) "
        "VALUES (%s, %s, 'active') RETURNING member_id", (employee_code, display_name)
    ).fetchone()["member_id"]


def _insert_org(conn, code, name, unit_type):
    return conn.execute(
        "INSERT INTO oams.org_units (org_code, name, unit_type) "
        "VALUES (%s, %s, %s) RETURNING org_unit_id", (code, name, unit_type)
    ).fetchone()["org_unit_id"]


def _insert_position(conn, code, org_unit_id):
    return conn.execute(
        "INSERT INTO oams.positions (position_code, title, org_unit_id) "
        "VALUES (%s, %s, %s) RETURNING position_id", (code, code + " 负责人", org_unit_id)
    ).fetchone()["position_id"]


def _insert_interface(conn, org_unit_id, member_id, position_id):
    return conn.execute(
        "INSERT INTO oams.interfaces "
        "(org_unit_id, slug, name, status, visibility, schema_version, descriptor, "
        " position_id, version_no, created_by, updated_by) "
        "VALUES (%s, 'status-fixture', '状态分布测试页', 'active', 'department', '0.2', "
        "        %s::jsonb, %s, 1, %s, %s) RETURNING interface_id",
        (org_unit_id, json.dumps(DESCRIPTOR), position_id, member_id, member_id)
    ).fetchone()["interface_id"]


def _insert_entry(conn, interface_id, member_id, status, word, *, entry_mode="live"):
    # 库自己不许 blocked 没有理由（migration 0005 的 CHECK），夹具照规矩给一个。
    blocked_reason = "等配件到货" if status == "blocked" else None
    return conn.execute(
        "INSERT INTO oams.interface_entries "
        "(interface_id, title, status, blocked_reason, evidence, reported_by, "
        " entry_mode, status_entry, extra) "
        "VALUES (%s, %s, %s, %s, 'SY-2410', %s, %s, 'active', %s::jsonb) RETURNING entry_id",
        (interface_id, "事项 " + word, status, blocked_reason, member_id, entry_mode,
         json.dumps({"state_word": word}))
    ).fetchone()["entry_id"]


def _insert_event(conn, entry_id, member_id):
    conn.execute(
        "INSERT INTO oams.workforce_audit_events "
        "(actor_member_id, event_type, subject_type, subject_id, reason) "
        "VALUES (%s, 'interface_entry.created', 'interface_entry', %s, '测试条目')",
        (member_id, entry_id))


@contextlib.contextmanager
def scratch_world(db_ready):
    with db_ready.connect() as conn:
        conn.execute("SAVEPOINT status_fixture")
        try:
            member_id = _insert_member(conn, "STATUSD01", "状态测试员")
            dept = _insert_org(conn, "STATUSD", "状态分布测试部门", "department")
            position = _insert_position(conn, "STATUSD_LEAD", dept)
            page = _insert_interface(conn, dept, member_id, position)
            parts = _insert_entry(conn, page, member_id, "blocked", "等配件")
            vendor = _insert_entry(conn, page, member_id, "blocked", "等厂商")
            fixed = _insert_entry(conn, page, member_id, "done", "已恢复")
            draft = _insert_entry(conn, page, member_id, "done", "已恢复", entry_mode="draft")
            _insert_event(conn, parts, member_id)
            yield {"conn": conn, "dept": dept, "page": page,
                   "entries": {"parts": parts, "vendor": vendor, "fixed": fixed,
                               "draft": draft}}
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT status_fixture")


@pytest.fixture()
def status_world(db_ready):
    with scratch_world(db_ready) as data:
        yield data


@pytest.mark.db
def test_aggregates_carry_the_four_standard_states_from_real_rows(status_world):
    panel = api.build_department_aggregates(status_world["conn"], status_world["dept"])
    aggregates = panel["aggregates"]
    assert aggregates["status.blocked"] == 2
    assert aggregates["status.done"] == 1, "草稿那条不该进分布图"
    assert aggregates.get("status.planned", 0) == 0


@pytest.mark.db
def test_aggregates_keep_the_two_words_that_share_a_state_apart(status_world):
    panel = api.build_department_aggregates(status_world["conn"], status_world["dept"])
    aggregates = panel["aggregates"]
    assert aggregates["status.等配件"] == 1
    assert aggregates["status.等厂商"] == 1
    assert aggregates["status.已恢复"] == 1, "草稿那条不算"


@pytest.mark.db
def test_aggregates_carry_activity_from_the_audit_table(status_world):
    panel = api.build_department_aggregates(status_world["conn"], status_world["dept"])
    activity = panel["aggregates"]["activity"]
    assert activity, "审计表里有条目事件，动态流不该空"
    assert activity[0]["what"] == "填写了页面条目"
    assert activity[0]["who"] == "状态测试员", "署名取审计事件里的 actor，不是别的机器上的管理员名"
    assert activity[0]["when"], "时间列必须带东西，不然模板印一行空的"


@pytest.mark.db
def test_no_activity_key_when_nothing_ever_happened(db_ready):
    """没有事件就不写这个键：模板那边保持空态，而不是收到一个空列表。"""
    with db_ready.connect() as conn:
        conn.execute("SAVEPOINT status_quiet")
        try:
            member_id = _insert_member(conn, "STATUSQ01", "安静测试员")
            dept = _insert_org(conn, "STATUSQ", "安静部门", "department")
            position = _insert_position(conn, "STATUSQ_LEAD", dept)
            _insert_interface(conn, dept, member_id, position)
            panel = api.build_department_aggregates(conn, dept)
            assert "activity" not in panel["aggregates"]
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT status_quiet")
