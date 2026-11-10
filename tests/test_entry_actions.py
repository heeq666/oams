"""记录上的动作（通过 / 驳回）：从页面点下去，真的改掉那条记录。

这是请假这类页面缺的那一层：页面自己声明「待批 → 已批」这个动作，人在记录上按一下，
状态就变，谁按的、从什么改成什么进审计。框架只认「页面声明了什么」，不认「这是请假」。
"""

from __future__ import annotations

import json
import re

import pytest

import oams.api
from test_review_preview import (  # noqa: F401  (fixture 由 pytest 收集)
    METRIC_DESCRIPTOR,
    _a_live_page,
    _fill_entry,
)
from test_review_stages import DESIGN_REVIEWER, room  # noqa: F401

pytestmark = pytest.mark.db

DESCRIPTOR = json.loads(json.dumps(METRIC_DESCRIPTOR))
DESCRIPTOR["status_map"]["被驳回"] = "blocked"
DESCRIPTOR["fields"][1]["options"] = ["进行中", "已完成", "被驳回"]
DESCRIPTOR["actions"] = [
    {"key": "approve", "label": "通过", "from": ["进行中"], "to": "已完成"},
    {"key": "reject", "label": "驳回", "from": ["进行中"], "to": "被驳回", "note": True},
]

#: 动作表单的令牌：只能从页面里取，不能手搓——取错了就说明模板发错了令牌。
_ACTION_TOKEN = re.compile(
    r'action="/departments/[^"]+/entries/[0-9a-f-]+/transition"[^>]*>.*?'
    r'name="csrf" value="([0-9a-f]+)"', re.S)


def _dept_html(room, interface_id) -> str:
    page = room["client"]().get(f"/departments/{room['design']}/{interface_id}")
    assert page.status_code == 200, page.text
    return page.text


def _a_record(room) -> tuple:
    """造一版上线页 + 一条「进行中」的记录，返回 (interface_id, entry_id)。"""
    interface_id = _a_live_page(room, descriptor=DESCRIPTOR)
    _fill_entry(room, interface_id, {"title": "王小明 事假",
                                     "state_word": "进行中",
                                     "done_count": "1",
                                     "evidence": "OA-2026-1112-07"})
    entry = room["conn"].execute(
        """SELECT entry_id, status, extra FROM oams.interface_entries
            WHERE interface_id = %s ORDER BY reported_at DESC LIMIT 1""",
        (interface_id,)).fetchone()
    return interface_id, entry["entry_id"]


def _press(room, interface_id, entry_id, *, action, note="", token=None):
    html = _dept_html(room, interface_id)
    found = _ACTION_TOKEN.search(html)
    assert found, "页面上没有动作表单：" + html[:400]
    return room["client"]().post(
        f"/departments/{room['design']}/{interface_id}/entries/{entry_id}/transition",
        data={"csrf": token or found.group(1), "action": action, "note": note},
        follow_redirects=False)


def _entry(room, entry_id):
    return room["conn"].execute(
        "SELECT status, extra FROM oams.interface_entries WHERE entry_id = %s",
        (entry_id,)).fetchone()


def _audit_count(room, entry_id) -> int:
    return room["conn"].execute(
        """SELECT count(*) AS n FROM oams.workforce_audit_events
            WHERE event_type = 'interface_entry.transitioned' AND subject_id = %s""",
        (entry_id,)).fetchone()["n"]


def test_the_buttons_offered_come_from_the_records_own_state(room):
    interface_id, entry_id = _a_record(room)
    html = _dept_html(room, interface_id)
    assert "通过" in html and "驳回" in html

    approved = _press(room, interface_id, entry_id, action="approve")
    assert approved.status_code == 303
    after = _dept_html(room, interface_id)
    # 已经「已完成」的记录不再提供「进行中 →」的那两个动作：按钮跟着状态走
    assert "驳回" not in after
    assert _ACTION_TOKEN.search(after) is None


def test_pressing_approve_moves_the_record_and_leaves_an_audit_line(room):
    interface_id, entry_id = _a_record(room)
    before = _audit_count(room, entry_id)

    pressed = _press(room, interface_id, entry_id, action="approve")
    assert pressed.status_code == 303, pressed.text
    assert "saved=transitioned" in pressed.headers["location"]

    row = _entry(room, entry_id)
    assert row["status"] == "done"
    assert row["extra"]["state_word"] == "已完成", row["extra"]
    assert _audit_count(room, entry_id) == before + 1


def test_a_button_left_open_cannot_be_pressed_twice(room):
    """浏览器后退再点一次：状态已经不是「进行中」，动作作废，记录不动。"""
    from urllib.parse import unquote

    interface_id, entry_id = _a_record(room)
    # 先把表单还开着时的那张令牌拿在手里——就是「用户按了后退」手里握着的东西
    stale = _ACTION_TOKEN.search(_dept_html(room, interface_id)).group(1)
    first = _press(room, interface_id, entry_id, action="approve")
    assert first.status_code == 303
    snapshot = _entry(room, entry_id)
    audit_before = _audit_count(room, entry_id)

    again = room["client"]().post(
        f"/departments/{room['design']}/{interface_id}/entries/{entry_id}/transition",
        data={"csrf": stale, "action": "approve"}, follow_redirects=False)
    assert again.status_code == 303
    assert "不能点这个动作" in unquote(again.headers["location"]), again.headers["location"]
    assert _entry(room, entry_id) == snapshot
    assert _audit_count(room, entry_id) == audit_before


def test_rejecting_without_a_reason_is_refused(room):
    interface_id, entry_id = _a_record(room)
    refused = _press(room, interface_id, entry_id, action="reject", note="")
    assert refused.status_code == 303
    from urllib.parse import unquote
    assert "要写一句原因" in unquote(refused.headers["location"])
    assert _entry(room, entry_id)["extra"]["state_word"] == "进行中"

    refused_note = _press(room, interface_id, entry_id, action="reject", note="病假条看不清")
    assert refused_note.status_code == 303
    row = _entry(room, entry_id)
    assert row["status"] == "blocked"
    assert row["extra"]["state_word"] == "被驳回"
    reason = room["conn"].execute(
        """SELECT reason FROM oams.workforce_audit_events
            WHERE event_type = 'interface_entry.transitioned' AND subject_id = %s
            ORDER BY occurred_at DESC LIMIT 1""",
        (entry_id,)).fetchone()["reason"]
    assert "病假条看不清" in reason


def test_a_token_minted_for_another_form_does_not_work(room):
    """「关联工作项」那张令牌不能拿来改状态：一张令牌只做一件事。"""
    import hashlib
    import hmac
    interface_id, entry_id = _a_record(room)
    link_token = hmac.new(b"review-stage-test", b"oams:entry-link:v1",
                          hashlib.sha256).hexdigest()
    refused = _press(room, interface_id, entry_id, action="approve", token=link_token)
    assert refused.status_code == 403
    assert _entry(room, entry_id)["extra"]["state_word"] == "进行中"


def test_a_record_from_another_page_cannot_be_moved_here(room):
    mine, _ = _a_record(room)
    other, other_entry = _a_record(room)
    token = _ACTION_TOKEN.search(_dept_html(room, mine)).group(1)
    refused = room["client"]().post(
        f"/departments/{room['design']}/{mine}/entries/{other_entry}/transition",
        data={"csrf": token, "action": "approve"}, follow_redirects=False)
    assert refused.status_code == 403
    assert _entry(room, other_entry)["extra"]["state_word"] == "进行中"
