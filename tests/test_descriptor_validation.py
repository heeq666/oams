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

from workhub.descriptor import (  # noqa: E402
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
# Visibility: public needs a recorded approval
# ---------------------------------------------------------------------------

def test_public_template_needs_approval():
    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    assert any("status=approved" in p for p in problems(descriptor))


def test_approved_public_template_allowed():
    descriptor = base()
    descriptor["meta"]["visibility"] = "public"
    descriptor["meta"]["review"] = {"status": "approved", "reviewed_by": "GM",
                                    "reviewed_at": "2026-10-13"}
    assert problems(descriptor) == []
