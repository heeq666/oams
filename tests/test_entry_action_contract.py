"""记录上的动作：契约层怎么判（不碰库）。

部门说的「通过」「驳回」是**动作**，不是系统状态名。所以动作的起止词必须是这一页
`status_map` 里已经出现过的词——一份词表，两处用，不会漂移。
"""

from __future__ import annotations

import copy
import json

from oams import descriptor as contract

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "leave-sample",
    "name": "请假登记样张",
    "org_unit_code": "GA",
    "position_codes": ["GA_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "list", "type": "progress_row", "params": {}}],
    "fields": [
        {"key": "title", "label": "谁请", "type": "text", "required": True,
         "map_to": "title", "aggregatable": False, "example": "王小明"},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "example": "待批",
         "options": ["待批", "已批", "被驳回"]},
        {"key": "evidence", "label": "依据", "type": "text", "required": False,
         "map_to": "evidence", "aggregatable": False, "example": "OA-2026-1112-07"},
    ],
    "status_map": {"待批": "planned", "已批": "done", "被驳回": "blocked"},
    "rules": {"evidence_required": False},
    "actions": [
        {"key": "approve", "label": "通过", "from": ["待批"], "to": "已批"},
        {"key": "reject", "label": "驳回", "from": ["待批"], "to": "被驳回", "note": True},
    ],
    "meta": {"author_position_code": "GA_LEAD", "version": 1,
             "visibility": "department", "review": {"status": "draft"}},
}


def _problems(document):
    return [p for p in contract.validate_descriptor(document) if p.startswith("[ACTION")]


def _with(actions):
    document = copy.deepcopy(DESCRIPTOR)
    document["actions"] = actions
    return document


def test_actions_in_the_departments_own_words_pass():
    assert _problems(DESCRIPTOR) == []
    # 也真的是能渲染的一页：别的错一个都没有
    assert contract.validate_descriptor(DESCRIPTOR) == []


def test_a_page_without_actions_is_still_valid():
    document = copy.deepcopy(DESCRIPTOR)
    document.pop("actions")
    assert contract.validate_descriptor(document) == []
    assert contract.entry_actions(document) == []


def test_an_action_that_names_a_state_the_department_never_mapped_is_rejected():
    document = _with([{"key": "approve", "label": "通过", "from": ["待批"], "to": "批了"}])
    problems = _problems(document)
    assert any("ACTION_UNKNOWN_STATE" in p and "actions[0].to" in p for p in problems), problems


def test_an_action_that_starts_from_an_unknown_word_is_rejected():
    document = _with([{"key": "approve", "label": "通过", "from": ["等着"], "to": "已批"}])
    problems = _problems(document)
    assert any("ACTION_UNKNOWN_STATE" in p and "actions[0].from" in p for p in problems), problems


def test_an_action_that_does_not_move_the_record_is_rejected():
    """from 和 to 是同一个词 = 点一下什么都不变，这种按钮不该存在。"""
    document = _with([{"key": "approve", "label": "通过", "from": ["待批"], "to": "待批"}])
    problems = _problems(document)
    assert any("must move the record" in p for p in problems), problems


def test_the_same_action_key_twice_is_rejected():
    document = _with([
        {"key": "approve", "label": "通过", "from": ["待批"], "to": "已批"},
        {"key": "approve", "label": "批准", "from": ["待批"], "to": "已批"},
    ])
    problems = _problems(document)
    assert any("ACTION_DUPLICATE_KEY" in p for p in problems), problems


def test_more_actions_than_the_limit_is_rejected():
    too_many = [{"key": f"a{i}", "label": f"动作{i}", "from": ["待批"], "to": "已批"}
                for i in range(contract.ENTRY_ACTION_LIMIT + 1)]
    problems = _problems(_with(too_many))
    assert any("ACTION_TOO_MANY" in p for p in problems), problems


def test_the_helpers_read_the_document_the_same_way_the_route_does():
    approve = contract.action_for(DESCRIPTOR, "approve")
    assert approve is not None
    assert contract.action_applies(approve, "待批") is True
    assert contract.action_applies(approve, "已批") is False
    assert contract.action_target_status(DESCRIPTOR, approve) == "done"
    assert contract.action_for(DESCRIPTOR, "withdraw") is None
