"""Checks for the interface descriptor validator: the gate AI output must pass.

Every case below is a frozen fact about the validator, so each one is its own
test: a regression shows up as that one red test and never as a collection-time
crash that hides the other ~600 tests in the suite.

``base()`` is deliberately the same descriptor the legacy script used.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.descriptor import (  # noqa: E402
    DescriptorError,
    assert_descriptor,
    validate_descriptor,
)

KNOWN = {"known_org_unit_codes": {"DESIGN"}, "known_position_codes": {"DESIGN_LEAD"}}


def base(**overrides):
    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-sampling",
        "name": "设计部打样进度",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "modules": [
            {"key": "item", "type": "item_card", "title": "事项", "order": 1,
             "source_fields": ["title"], "aggregatable": True},
            {"key": "progress", "type": "progress_bar", "title": "打样进度", "order": 2,
             "source_fields": ["sample_done"], "aggregatable": True},
            {"key": "evidence", "type": "evidence_input", "title": "依据", "order": 3,
             "source_fields": ["evidence_ref"], "aggregatable": True},
            {"key": "state", "type": "metric_card", "title": "状态", "order": 4,
             "source_fields": ["state_word"], "aggregatable": True},
        ],
        "fields": [
            {"key": "title", "label": "款号", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "AW24-001"},
            {"key": "sample_done", "label": "完成打样数", "type": "number", "required": True,
             "map_to": "progress_value", "aggregatable": True, "example": "12"},
            {"key": "evidence_ref", "label": "打样单号", "type": "text", "required": True,
             "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
            {"key": "state_word", "label": "打样状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "打样中"},
            {"key": "fabric_note", "label": "面料备注", "type": "text", "required": False,
             "map_to": "display_only", "aggregatable": False, "example": "待补货"},
        ],
        "status_map": {"待打样": "planned", "打样中": "in_progress", "等面料": "blocked", "已完成": "done"},
        "rules": {"evidence_required": True, "progress_range": [0, 100]},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }
    descriptor.update(overrides)
    return descriptor


def problems(descriptor) -> list[str]:
    """Problems as plain strings.

    v0.1 documents are uncoded, so every entry must already be a string; turning
    a non-string into a visible marker keeps a leakage bug (a tuple or dict in the
    list) from silently reading as "no problem found".
    """
    return [p if isinstance(p, str) else f"<{type(p).__name__}> {p!r}"
            for p in validate_descriptor(descriptor, **KNOWN)]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_descriptor_passes():
    assert problems(base()) == []


def test_assert_descriptor_passes_silently():
    assert_descriptor(base(), **KNOWN)


# ---------------------------------------------------------------------------
# Structural rejections
# ---------------------------------------------------------------------------

def test_missing_required_key_rejected():
    descriptor = base()
    del descriptor["name"]
    assert any(p.startswith("name:") for p in problems(descriptor))


def test_unknown_department_rejected():
    assert any("unknown department" in p for p in problems(base(org_unit_code="NOPE")))


def test_assert_descriptor_raises_with_reasons():
    with pytest.raises(DescriptorError) as excinfo:
        assert_descriptor(base(org_unit_code="NOPE"), **KNOWN)
    assert "unknown department" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Field vocabulary and aggregation
# ---------------------------------------------------------------------------

def test_display_only_cannot_aggregate():
    descriptor = base()
    descriptor["fields"][4] = {"key": "fabric_note", "label": "面料备注", "type": "text",
                               "map_to": "display_only", "aggregatable": True, "example": "x"}
    assert any("display_only requires aggregatable=false" in p for p in problems(descriptor))


def test_unknown_map_to_rejected():
    descriptor = base()
    descriptor["fields"][1]["map_to"] = "vibes"
    assert any("not a standard field" in p for p in problems(descriptor))


def test_non_standard_status_rejected():
    descriptor = base(status_map={"打样中": "in_progress", "等面料": "waiting"})
    assert any("not one of" in p for p in problems(descriptor))


def test_unknown_source_field_rejected():
    descriptor = base()
    descriptor["modules"][0]["source_fields"] = ["ghost"]
    assert any("unknown field" in p for p in problems(descriptor))


def test_interface_without_evidence_cannot_aggregate():
    descriptor = base()
    descriptor["fields"] = [f for f in descriptor["fields"] if f["map_to"] != "evidence"]
    assert any("cannot be summarised" in p for p in problems(descriptor))


# ---------------------------------------------------------------------------
# Custom modules: a purpose, never logic
# ---------------------------------------------------------------------------

def test_constrained_custom_module_allowed():
    descriptor = base()
    descriptor["modules"].append({
        "key": "sampling_board", "type": "custom", "title": "打样看板", "order": 5,
        "source_fields": ["title"],
        "custom": {"purpose": "款号与状态一起看", "composed_of": ["list_block", "progress_bar"],
                   "display": "table", "declares_logic": False},
    })
    assert problems(descriptor) == []


def test_custom_module_declaring_logic_rejected():
    descriptor = base()
    descriptor["modules"].append({
        "key": "rogue", "type": "custom", "title": "抓取", "order": 6, "source_fields": ["title"],
        "custom": {"purpose": "抓外部数据", "composed_of": ["list_block"], "display": "table",
                   "declares_logic": True},
    })
    assert any("declares_logic must be false" in p for p in problems(descriptor))


def test_smuggled_script_rejected():
    """A descriptor is data: no key may smuggle an executable payload in."""
    descriptor = base()
    descriptor["modules"][0]["script"] = "import os; os.system('rm -rf /')"
    found = problems(descriptor)
    assert any("forbidden key" in p for p in found), found


# ---------------------------------------------------------------------------
# Visibility: public is a request; the strict check guards the doors that skip review
# ---------------------------------------------------------------------------

def test_public_template_can_be_declared_but_not_installed_unsigned():
    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    # Asking for a public page is not a problem -- refusing the ask is what made
    # departments give up on it. The signature is what the strict door wants.
    assert problems(descriptor) == []
    assert any("status=approved" in p
               for p in validate_descriptor(descriptor, require_recorded_approval=True))


def test_approved_public_template_allowed():
    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    descriptor["meta"]["review"] = {"status": "approved", "reviewed_by": "GM",
                                    "reviewed_at": "2026-10-13"}
    assert problems(descriptor) == []
    assert validate_descriptor(descriptor, require_recorded_approval=True) == []


def test_a_stamped_descriptor_reads_back_as_approved():
    """The stamp has to survive the round trip to JSON, because that is what the
    database stores and what the next load reads: a signature that only exists in
    the request handler is not a record."""
    import json

    from oams.review import stamp_approval

    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    stamped = stamp_approval(descriptor, reviewed_by="DESIGN001", stage="department",
                             reviewed_at="2026-10-31T09:00:00Z", version_no=3)
    reloaded = json.loads(json.dumps(stamped))

    assert reloaded["meta"]["review"] == {
        "status": "approved", "reviewed_by": "DESIGN001",
        "reviewed_at": "2026-10-31T09:00:00Z", "stage": "department", "version_no": 3}
    assert validate_descriptor(reloaded, require_recorded_approval=True) == []
    # The author's own copy is untouched: stamping is not an in-place edit that a
    # caller could be surprised by.
    assert descriptor["meta"]["review"] != reloaded["meta"]["review"]


def test_a_status_word_that_is_not_approved_is_not_a_signature():
    """回归：`{"status": "draft"}` 曾经因为「非空即算填」被读成已批准。

    签名是三个人一组的东西——谁签、什么时候、签的什么结论。前两个字段填满不代表
    第三个字段成立：一页自己写着 draft，就是作者在说「还没批」。

    这里故意用**部门可见 + 公司汇总能力**：这条判据没有「公开」那条兜底，缺口报不报
    全看「三个人一组齐没齐」，所以它才是那个 bug 的原发地——旧口径下 draft 被当成
    填好了，一页没批的公司汇总页就这么上线了。
    """
    from oams.descriptor import recorded_approval_gaps

    descriptor = base()
    descriptor["capabilities"] = ["entries:write", "aggregate:read:company"]
    # 谁签、什么时候都填满了，只有结论写着 draft。
    descriptor["meta"]["review"] = {"status": "draft", "reviewed_by": "GM",
                                    "reviewed_at": "2026-10-13"}

    gaps = recorded_approval_gaps(descriptor)
    assert gaps, "draft 不算批准，缺口必须报出来"
    assert any("second-level approval" in gap for gap in gaps)
    assert validate_descriptor(descriptor, require_recorded_approval=True) != []

    # 只把结论改成 approved，其余一个字不动，缺口就该消失——证明上面那条不是空跑。
    descriptor["meta"]["review"]["status"] = "approved"
    assert recorded_approval_gaps(descriptor) == []


def test_a_hand_written_signature_must_name_somebody_who_works_here():
    """签名是组织里的事实，不是文件里的一串字。

    「approve 写在 JSON 里」上一轮已经堵住了；剩下半个洞是「谁签」——作者手写一个
    `reviewed_by: 王二`，形状上完全合规，只有连库才知道公司里有没有这个人。所以这一
    条分开验：不给名单（离线）时只验形状，给名单时连名字一起验。
    """
    from oams.descriptor import recorded_approval_gaps

    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    descriptor["meta"]["review"] = {"status": "approved", "reviewed_by": "王二",
                                    "reviewed_at": "2026-10-31T09:00:00Z"}

    # 离线：形状齐全，这条查不了，不硬报。
    assert recorded_approval_gaps(descriptor) == []
    # 连库：名单里没有「王二」，当场算缺口。
    gaps = recorded_approval_gaps(descriptor, known_employee_codes={"DESIGN001", "ADMIN001"})
    assert len(gaps) == 1
    assert "must name an active member" in gaps[0]
    assert "王二" in gaps[0]

    strict = validate_descriptor(descriptor, require_recorded_approval=True,
                                 known_employee_codes={"DESIGN001"})
    assert len(strict) == 1 and "active member" in strict[0]

    # 换成真在册的工号，同一条就过——证明上面不是「有名单就报」。
    descriptor["meta"]["review"]["reviewed_by"] = "DESIGN001"
    assert recorded_approval_gaps(descriptor, known_employee_codes={"DESIGN001"}) == []
    assert validate_descriptor(descriptor, require_recorded_approval=True,
                               known_employee_codes={"DESIGN001"}) == []


def test_an_unverifiable_name_is_not_reported_as_verified_offline():
    """离线不是「通过」，是「没验」。函数返回空列表只代表形状齐、不代表签字人是真的；
    两件事由调用方在报告里分开说，这里钉住「给不给名单结果确实不同」。"""
    from oams.descriptor import recorded_approval_gaps

    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    descriptor["meta"]["review"] = {"status": "approved", "reviewed_by": "CTO",
                                    "reviewed_at": "2026-10-31T09:00:00Z"}

    assert recorded_approval_gaps(descriptor) == []
    assert recorded_approval_gaps(descriptor, known_employee_codes=set()) != []
