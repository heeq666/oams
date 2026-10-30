"""Descriptor v0.2 acceptance tests (contract §1-§5).

Covers the two things the first release could not express: a nested container
layout with a container-depth budget, and a closed vocabulary for fields,
capabilities and metrics. Each problem must be reported *with a reject code for a
human to filter on*, and every problem must be collected, not just the first.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub.descriptor import (  # noqa: E402
    CAPABILITIES,
    CONTAINER_DEPTH_LIMIT,
    CURRENT_SCHEMA_VERSION,
    REJECT_CODES,
    SCHEMA_VERSIONS,
    PLATFORM_DERIVED_FIELDS,
    STANDARD_FIELD_VOCAB,
    DescriptorError,
    assert_descriptor,
    layout_depth,
    validate_descriptor,
)

KNOWN = {"known_org_unit_codes": {"PRODUCTION"}, "known_position_codes": {"PRODUCTION_LEAD"}}


def field(key, map_to, type_="text", aggregatable=True, **extra):
    payload = {
        "key": key,
        "label": key,
        "type": type_,
        "required": False,
        "map_to": map_to,
        "aggregatable": aggregatable,
        "example": "x",
    }
    payload.update(extra)
    return payload


def base(**overrides):
    """A minimal descriptor that validates cleanly; tests break one thing at a time."""
    descriptor = {
        "schema_version": "0.2",
        "interface_id": "production-sampling",
        "name": "生产部打样进度",
        "org_unit_code": "PRODUCTION",
        "position_codes": ["PRODUCTION_LEAD"],
        "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
        "modules": [
            {
                "key": "board",
                "type": "columns",
                "params": {"columns": 2},
                "children": [
                    {
                        "key": "left",
                        "type": "card",
                        "params": {"title": "本周打样"},
                        "children": [
                            {
                                "key": "rows",
                                "type": "progress_row",
                                "module_version": "^1.0",
                                "source_fields": ["style_no", "stage", "percent_done", "sample_sheet"],
                            },
                        ],
                    },
                ],
            },
        ],
        "fields": [
            field("style_no", "title"),
            field("stage", "status", "enum"),
            field("percent_done", "progress_value", "percent"),
            field("sample_sheet", "evidence"),
            field("fabric_note", "display_only", aggregatable=False),
        ],
        "status_map": {"待排产": "planned", "打样中": "in_progress", "已完成": "done"},
        "metrics": [{"metric_key": "funnel.sample_done", "mode": "display"}],
        "rules": {"evidence_required": True},
        "meta": {"visibility": "department", "review": {"status": "draft"}},
    }
    descriptor.update(overrides)
    return descriptor


def codes(problems):
    return [p.split("]")[0].lstrip("[") for p in problems]


def test_constants_are_frozen():
    assert SCHEMA_VERSIONS == ("0.1", "0.2")
    assert CURRENT_SCHEMA_VERSION == "0.2"
    assert len(CAPABILITIES) == 7
    assert REJECT_CODES == (
        "FIELD_UNMAPPED", "STATUS_UNMAPPED", "METRIC_UNDEFINED", "CAPABILITY_ESCALATION",
        "MODULE_NOT_WHITELISTED", "LAYOUT_TOO_DEEP", "DATA_RULE_VIOLATION",
    )


def test_reference_fixture_is_valid():
    assert validate_descriptor(base()) == []
    assert assert_descriptor(base()) is None
    assert layout_depth(base()) == 2


# --------------------------------------------------------------------------- #
# Container depth (contract §1: at most two nested containers, leaves excluded)
# --------------------------------------------------------------------------- #


def test_legal_shape_columns_card_progress_row():
    descriptor = base()
    assert layout_depth(descriptor) == 2
    assert validate_descriptor(descriptor) == []


def test_over_limit_shape_columns_card_grid_is_rejected():
    descriptor = base()
    descriptor["modules"] = [
        {
            "key": "board",
            "type": "columns",
            "children": [
                {"key": "left", "type": "card", "params": {"title": "本周打样"}, "children": [
                    {"key": "inner", "type": "grid", "children": [
                        {"key": "rows", "type": "progress_row",
                         "source_fields": ["style_no", "stage", "sample_sheet"]},
                    ]},
                ]},
            ],
        },
    ]
    assert layout_depth(descriptor) == CONTAINER_DEPTH_LIMIT + 1 == 3
    problems = validate_descriptor(descriptor)
    assert codes(problems) == ["LAYOUT_TOO_DEEP"]
    assert problems[0].startswith(
        "[LAYOUT_TOO_DEEP] modules[board].children[left].children[inner]:"
    )
    assert "container level 3 exceeds the limit of 2" in problems[0]


def test_two_flat_containers_are_depth_two_not_three():
    descriptor = base()
    descriptor["modules"] = [
        {"key": "outer", "type": "rows", "children": [
            {"key": "inner", "type": "grid", "children": [
                {"key": "rows", "type": "progress_row",
                 "source_fields": ["style_no", "stage", "sample_sheet"]},
            ]},
        ]},
    ]
    assert layout_depth(descriptor) == 2
    assert validate_descriptor(descriptor) == []


# --------------------------------------------------------------------------- #
# Reject codes and message shape
# --------------------------------------------------------------------------- #


def test_v02_problems_carry_their_code_but_v01_does_not():
    broken = base(fields=[field("style_no", "colour_hex")])
    problems = validate_descriptor(broken)
    assert problems
    assert all(p.startswith("[") and "] " in p for p in problems)
    assert "FIELD_UNMAPPED" in codes(problems)


def test_unknown_map_to_is_field_unmapped():
    broken = base()
    broken["fields"] = [field("style_no", "not_a_standard_field")]
    problems = validate_descriptor(broken)
    assert "FIELD_UNMAPPED" in codes(problems)
    assert any("not_a_standard_field" in p for p in problems)


def test_display_only_must_not_be_aggregatable():
    broken = base()
    broken["fields"].append(field("private", "display_only", aggregatable=True))
    problems = validate_descriptor(broken)
    assert "DATA_RULE_VIOLATION" in codes(problems)
    assert any("aggregatable=false" in p for p in problems)


def test_unknown_status_target_is_status_unmapped():
    broken = base(status_map={"打样中": "ongoing"})
    problems = validate_descriptor(broken)
    assert codes(problems) == ["STATUS_UNMAPPED"]


def test_aggregate_needs_title_status_and_evidence():
    for missing in ("title", "status", "evidence"):
        broken = base()
        broken["fields"] = [f for f in broken["fields"] if f["map_to"] != missing]
        problems = validate_descriptor(broken)
        assert "FIELD_UNMAPPED" in codes(problems), missing
        assert any("aggregate" in p and missing in p for p in problems), missing


def test_forbidden_keys_are_rejected_anywhere():
    broken = base()
    broken["modules"][0]["children"][0]["params"] = {"title": "x", "script": "rm -rf /"}
    problems = validate_descriptor(broken)
    assert any("forbidden key" in p for p in problems)


def test_unknown_module_type_is_rejected():
    broken = base()
    broken["modules"][0]["children"][0]["children"][0]["type"] = "item_card"
    problems = validate_descriptor(broken)
    assert "MODULE_NOT_WHITELISTED" in codes(problems)


def test_required_param_missing_is_rejected():
    broken = base()
    broken["modules"][0]["children"][0]["children"].append(
        {"key": "note", "type": "note_block"}
    )
    problems = validate_descriptor(broken)
    assert "DATA_RULE_VIOLATION" in codes(problems)
    assert any("required param 'text'" in p for p in problems)


# --------------------------------------------------------------------------- #
# Capabilities (default deny)
# --------------------------------------------------------------------------- #


def test_unknown_capability_is_escalation():
    broken = base(capabilities=["entries:write", "entries:delete:any"])
    problems = validate_descriptor(broken)
    assert codes(problems) == ["CAPABILITY_ESCALATION"]
    assert "entries:delete:any" in problems[0]


def test_company_aggregate_needs_recorded_approval():
    unapproved = base(capabilities=["aggregate:read:company"])
    problems = validate_descriptor(unapproved)
    assert codes(problems) == ["CAPABILITY_ESCALATION"]
    assert "second-level approval" in problems[0]

    approved = base(
        capabilities=["aggregate:read:company"],
        meta={"visibility": "department",
              "review": {"status": "approved", "reviewed_by": "CTO", "reviewed_at": "2026-01-01"}},
    )
    assert validate_descriptor(approved) == []


def test_duplicate_capability_is_a_data_rule_violation():
    broken = base(capabilities=["entries:write", "entries:write"])
    problems = validate_descriptor(broken)
    assert codes(problems) == ["DATA_RULE_VIOLATION"]
    assert "declared twice" in problems[0]


def test_public_visibility_requires_approval():
    broken = base(meta={"visibility": "public", "review": {"status": "draft"}})
    problems = validate_descriptor(broken)
    assert codes(problems) == ["DATA_RULE_VIOLATION"] * len(problems)
    assert len(problems) == 3


# --------------------------------------------------------------------------- #
# Metrics (contract §2)
# --------------------------------------------------------------------------- #


def test_display_metric_must_be_platform():
    broken = base(metrics=[{"metric_key": "dept.PRODUCTION.x", "mode": "display"}])
    problems = validate_descriptor(broken)
    assert codes(problems) == ["METRIC_UNDEFINED"]


def test_display_module_param_must_reference_a_platform_metric():
    broken = base()
    broken["modules"][0]["children"][0]["children"].append(
        {"key": "cards", "type": "metric_card", "params": {"metric_key": "dept.PRODUCTION.x"}}
    )
    problems = validate_descriptor(broken)
    assert codes(problems) == ["METRIC_UNDEFINED"]
    assert "dept.PRODUCTION.x" in problems[0]


def test_emit_metric_must_use_the_department_namespace():
    broken = base(metrics=[{"metric_key": "funnel.gmv", "mode": "display"},
                           {"metric_key": "other.x", "mode": "emit", "agg": "count",
                            "from_field": "stage"}])
    problems = validate_descriptor(broken)
    assert "DATA_RULE_VIOLATION" in codes(problems)
    assert any("namespace" in p for p in problems)


def test_duplicate_metric_binding_is_reported():
    broken = base(metrics=[{"metric_key": "funnel.gmv", "mode": "display"},
                           {"metric_key": "funnel.gmv", "mode": "display"}])
    problems = validate_descriptor(broken)
    assert any("duplicate metric binding" in p for p in problems)


# --------------------------------------------------------------------------- #
# Schema-version behaviour
# --------------------------------------------------------------------------- #


def test_v01_document_may_not_carry_v02_features():
    legacy = base(schema_version="0.1")
    problems = validate_descriptor(legacy)
    assert problems
    messages = " | ".join(problems)
    assert "capabilities" in messages and "0.2 feature" in messages
    assert "metrics" in messages


def test_strict_v02_field_vocabulary():
    # A v0.1-only mapping ("org_unit_id") is not in the §5 vocabulary.
    descriptor = base()
    descriptor["fields"].append(field("org", "org_unit_id"))
    assert "FIELD_UNMAPPED" in codes(validate_descriptor(descriptor))
    # ... but the migration window tolerates it.
    assert validate_descriptor(descriptor, strict_v02=False) == []


def test_bad_schema_version_is_rejected():
    problems = validate_descriptor(base(schema_version="9.9"))
    assert codes(problems) == ["DATA_RULE_VIOLATION"]
    assert "schema_version" in problems[0]


def test_non_dict_descriptor_is_rejected():
    assert validate_descriptor(["nope"]) == ["descriptor must be a JSON object"]
    assert validate_descriptor(None) == ["descriptor must be a JSON object"]


def test_missing_required_scope_keys():
    broken = base()
    del broken["interface_id"]
    del broken["rules"]
    problems = validate_descriptor(broken)
    assert codes(problems) == ["DATA_RULE_VIOLATION", "DATA_RULE_VIOLATION"]
    assert any("interface_id" in p for p in problems)
    assert any("rules" in p for p in problems)


def test_known_department_and_position_are_checked():
    problems = validate_descriptor(
        base(org_unit_code="MARKETING", position_codes=["GHOST"]), **KNOWN
    )
    assert "DATA_RULE_VIOLATION" in codes(problems)
    messages = " | ".join(problems)
    assert "unknown department 'MARKETING'" in messages
    assert "unknown position 'GHOST'" in messages


def test_assert_descriptor_raises_with_every_reason():
    broken = base(org_unit_code="MARKETING", position_codes=["GHOST"])
    with pytest.raises(DescriptorError) as excinfo:
        assert_descriptor(broken, **KNOWN)
    assert "unknown department" in str(excinfo.value)
    assert "unknown position" in str(excinfo.value)


def test_layout_depth_helper_ignores_missing_modules():
    assert layout_depth({"schema_version": "0.2"}) == 0


def test_standard_field_vocab_is_the_only_map_to_source():
    assert "title" in STANDARD_FIELD_VOCAB
    for field_key in ("style_no", "stage"):
        assert field_key not in STANDARD_FIELD_VOCAB


# --------------------------------------------------------------------------- #
# Platform-derived fields (user ruling: style-level sample status is derived
# from the colour-level development records, never typed by a department)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("map_to", ["sample_status", "approval_status"])
def test_platform_derived_map_to_is_rejected(map_to):
    broken = base()
    broken["fields"].append(field("platform_state", map_to, "enum"))
    problems = validate_descriptor(broken)
    assert "DATA_RULE_VIOLATION" in codes(problems)
    hit = next(p for p in problems if map_to in p)
    # The code prefix comes first, then the path, exactly like every other reject.
    assert hit.startswith(f"[DATA_RULE_VIOLATION] fields[platform_state]:")
    assert map_to in hit
    assert "颜色级记录派生" in hit and "部门不得填报" in hit
    assert "display_only" in hit


@pytest.mark.parametrize("map_to", ["sample_status", "approval_status"])
def test_platform_derived_reject_has_no_other_complaint(map_to):
    # One broken mapping must produce exactly one problem: no bogus vocab error.
    broken = base()
    broken["fields"].append(field("platform_state", map_to, "enum"))
    assert codes(validate_descriptor(broken)) == ["DATA_RULE_VIOLATION"]


@pytest.mark.parametrize("map_to", ["sample_status", "approval_status"])
def test_platform_derived_names_stay_in_the_vocabulary(map_to):
    # They are still canonical names the platform writes; only *map_to* is banned.
    assert map_to in STANDARD_FIELD_VOCAB
    assert map_to in PLATFORM_DERIVED_FIELDS


def test_map_to_status_is_still_legal():
    descriptor = base()
    descriptor["fields"] = [
        field("style_no", "title"),
        field("stage", "status", "enum"),
        field("percent_done", "progress_value", "percent"),
        field("sample_sheet", "evidence"),
    ]
    assert validate_descriptor(descriptor) == []


def test_display_only_is_unaffected_by_the_platform_derived_rule():
    descriptor = base()
    descriptor["fields"].append(
        field("sample_state_note", "display_only", aggregatable=False)
    )
    assert validate_descriptor(descriptor) == []


def test_platform_derived_rule_applies_to_v01_documents_too():
    # A 0.1 document that maps sample_status is just as wrong, and the code prefix
    # is dropped for 0.1 -- but the complaint must still be there.
    legacy = base(schema_version="0.1")
    legacy.pop("capabilities")
    legacy.pop("metrics")
    legacy["fields"].append(field("platform_state", "sample_status", "enum"))
    problems = validate_descriptor(legacy)
    assert any("颜色级记录派生" in p for p in problems)


def test_migration_window_also_rejects_platform_derived_map_to():
    descriptor = base()
    descriptor["fields"].append(field("platform_state", "sample_status", "enum"))
    assert any("颜色级记录派生" in p
               for p in validate_descriptor(descriptor, strict_v02=False))


# --------------------------------------------------------------------------- #
# Param shape: flat keys are honoured, out-of-range values are refused
# --------------------------------------------------------------------------- #


def table_descriptor(**node):
    """base() with the card's child replaced by one `table` node."""
    descriptor = base()
    table = {
        "key": "detail",
        "type": "table",
        "params": {"columns": [{"key": "style_no", "label": "款号"}]},
        "source_fields": ["style_no", "stage"],
    }
    table.update(node)
    descriptor["modules"][0]["children"][0]["children"] = [table]
    return descriptor


def test_table_rows_written_flat_is_valid():
    """`rows: 3` next to params.columns used to be dropped without a word."""
    descriptor = table_descriptor(rows=3)
    assert validate_descriptor(descriptor) == []


def test_table_rows_out_of_range_is_refused_not_clamped():
    problems = validate_descriptor(table_descriptor(rows=9))
    assert codes(problems) == ["DATA_RULE_VIOLATION"]
    assert problems[0].startswith("[DATA_RULE_VIOLATION] modules[board].children[left]"
                                  ".children[detail].rows:")
    assert "at most 5" in problems[0]

    nested = table_descriptor()
    nested["modules"][0]["children"][0]["children"][0]["params"]["rows"] = 0
    nested_problems = validate_descriptor(nested)
    assert codes(nested_problems) == ["DATA_RULE_VIOLATION"]
    assert nested_problems[0].endswith("must be at least 1, got 0")


def test_an_undeclared_node_key_is_refused_not_ignored():
    problems = validate_descriptor(table_descriptor(rowz=3))
    assert codes(problems) == ["DATA_RULE_VIOLATION"]
    assert "unknown key(s) ['rowz']" in problems[0]
    assert "declared params ['columns', 'rows']" in problems[0]
