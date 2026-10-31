"""Module registry acceptance tests (contract §1, frozen 20-module table).

Every assertion here is a frozen fact from docs/architecture/p0-interface-contract.md
§1: the key set, the layer split, what a container may nest, and how layout depth is
counted (containers only -- leaves never add a level).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.modules import (  # noqa: E402
    CONTAINER_DEPTH_LIMIT,
    LAYERS,
    MODULE_REGISTRY,
    STANDARD_FIELD_VOCAB,
    STANDARD_STATUSES,
    default_module_version,
    get_module,
    list_modules,
    module_depth,
    module_params,
    module_version_matches,
    param_problem,
    validate_module_tree,
    written_param_names,
)

CONTAINER_KEYS = {"columns", "rows", "grid", "card", "tabs", "divider", "spacer"}
INPUT_KEYS = {
    "progress_row", "table", "timeline", "blocker_list", "metric_input", "evidence_gallery",
}
DISPLAY_KEYS = {
    "metric_card", "metric_grid", "flow_strip", "status_chart", "rank_list",
    "activity_feed", "note_block",
}


# --------------------------------------------------------------------------- #
# The frozen table
# --------------------------------------------------------------------------- #


def test_registry_holds_exactly_the_twenty_frozen_keys():
    assert set(MODULE_REGISTRY) == CONTAINER_KEYS | INPUT_KEYS | DISPLAY_KEYS
    assert len(MODULE_REGISTRY) == 20


def test_layer_split_is_seven_six_seven():
    assert LAYERS == ("container", "input", "display")
    by_layer: dict[str, set[str]] = {layer: set() for layer in LAYERS}
    for spec in MODULE_REGISTRY.values():
        by_layer[spec.layer].add(spec.key)
    assert by_layer["container"] == CONTAINER_KEYS
    assert by_layer["input"] == INPUT_KEYS
    assert by_layer["display"] == DISPLAY_KEYS
    assert (len(by_layer["container"]), len(by_layer["input"]), len(by_layer["display"])) == (7, 6, 7)


def test_only_containers_can_nest():
    for spec in MODULE_REGISTRY.values():
        assert spec.can_nest is (spec.layer == "container"), spec.key


def test_aggregatable_flags():
    for spec in MODULE_REGISTRY.values():
        if spec.layer in ("container", "display"):
            assert spec.aggregatable is None, spec.key
    assert get_module("progress_row").aggregatable is True
    assert get_module("table").aggregatable is True
    assert get_module("timeline").aggregatable is True
    assert get_module("blocker_list").aggregatable is True
    assert get_module("metric_input").aggregatable is True
    # Evidence is stored but never rolled up into the company aggregate.
    assert get_module("evidence_gallery").aggregatable is False


def test_display_modules_consume_aggregates_not_fields():
    for key in DISPLAY_KEYS:
        spec = get_module(key)
        assert spec.consumes == (), key
        assert spec.produces == (), key


def test_standard_statuses_are_the_four_company_states():
    assert STANDARD_STATUSES == ("planned", "in_progress", "blocked", "done")


def test_standard_field_vocab_is_frozen_and_unique():
    assert len(STANDARD_FIELD_VOCAB) == 37
    assert len(set(STANDARD_FIELD_VOCAB)) == len(STANDARD_FIELD_VOCAB)
    # The seventh group (concept line 80) must be present or progress_row /
    # blocker_list produce columns that no field can map to.
    for key in ("blocked_reason", "needs_decision", "progress_value"):
        assert key in STANDARD_FIELD_VOCAB
    for key in ("title", "status", "evidence", "style_code", "percent"):
        assert key in STANDARD_FIELD_VOCAB


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def test_get_module_unknown_and_non_string():
    assert get_module("nope") is None
    assert get_module(None) is None
    assert get_module(42) is None
    assert get_module("item_card") is None  # legacy type, void in v0.2


def test_list_modules_filters_by_layer():
    assert len(list_modules()) == 20
    assert len(list_modules(None)) == 20
    assert len(list_modules("all")) == 20
    assert {m["key"] for m in list_modules("container")} == CONTAINER_KEYS
    assert {m["key"] for m in list_modules("input")} == INPUT_KEYS
    assert {m["key"] for m in list_modules("display")} == DISPLAY_KEYS
    with pytest.raises(ValueError):
        list_modules("widget")


def test_to_dict_is_json_serialisable_and_deep_copied():
    payload = list_modules()
    json.dumps(payload)  # the shape MCP list_modules returns must survive JSON
    first = payload[0]
    first["params"]["columns"] = {"type": "bogus"}
    assert MODULE_REGISTRY[first["key"]].params["columns"]["type"] == "int"


# --------------------------------------------------------------------------- #
# Depth semantics (contract §1 lines 78-96: containers only)
# --------------------------------------------------------------------------- #


def test_depth_counts_containers_only():
    legal = [{"key": "board", "type": "columns", "children": [
        {"key": "left", "type": "card", "children": [
            {"key": "rows", "type": "progress_row"},
        ]},
    ]}]
    assert module_depth(legal) == 2
    assert module_depth([]) == 0
    assert module_depth(None) == 0
    # A flat list of leaves carries no container at all.
    assert module_depth([{"key": "a", "type": "progress_row"}]) == 0
    assert module_depth([{"key": "a", "type": "columns"}]) == 1


def test_legal_two_container_nesting_produces_no_problem():
    legal = [{"key": "board", "type": "columns", "children": [
        {"key": "left", "type": "card", "children": [
            {"key": "rows", "type": "progress_row"},
        ]},
    ]}]
    assert validate_module_tree(legal) == []


def test_third_container_level_is_reported_as_layout_too_deep():
    over = [{"key": "board", "type": "columns", "children": [
        {"key": "left", "type": "card", "children": [
            {"key": "inner", "type": "grid", "children": [
                {"key": "rows", "type": "progress_row"},
            ]},
        ]},
    ]}]
    assert module_depth(over) == CONTAINER_DEPTH_LIMIT + 1 == 3
    problems = validate_module_tree(over)
    assert len(problems) == 1
    assert problems[0].startswith("[LAYOUT_TOO_DEEP] modules[board].children[left].children[inner]:")
    assert "container level 3 exceeds the limit of 2" in problems[0]


def test_rows_grid_depth_two_is_still_legal():
    shape = [{"key": "outer", "type": "rows", "children": [
        {"key": "inner", "type": "grid", "children": [
            {"key": "rows", "type": "progress_row"},
        ]},
    ]}]
    assert module_depth(shape) == 2
    assert validate_module_tree(shape) == []


# --------------------------------------------------------------------------- #
# Structural rejections
# --------------------------------------------------------------------------- #


def test_unknown_type_and_retired_custom():
    problems = validate_module_tree([{"key": "a", "type": "item_card"}])
    assert len(problems) == 1
    assert problems[0].startswith("[MODULE_NOT_WHITELISTED] modules[a]:")
    assert "item_card" in problems[0]

    retired = validate_module_tree([{"key": "b", "type": "custom"}])
    assert len(retired) == 1
    assert retired[0].startswith("[MODULE_NOT_WHITELISTED] modules[b]:")
    assert "retired" in retired[0]


def test_empty_and_non_list_module_lists():
    assert validate_module_tree([])[0].startswith("[MODULE_NOT_WHITELISTED] modules:")
    assert validate_module_tree("nope")[0].startswith("[MODULE_NOT_WHITELISTED] modules:")
    assert validate_module_tree([42])[0].startswith("[MODULE_NOT_WHITELISTED] modules[0]:")


def test_container_iron_law_rejects_data_and_text():
    carrying_data = validate_module_tree([
        {"key": "c", "type": "columns", "source_fields": ["a"], "params": {"text": "hi"}},
    ])
    joined = " | ".join(carrying_data)
    # Three distinct violations: source_fields is not an identity key at all, a
    # container must not reference data, and a container must not carry literal text.
    assert len(carrying_data) == 3
    assert "container may only carry children and identity keys" in joined
    assert "source_fields: a container must not reference data" in joined
    assert "params.text: a container must not carry" in joined

    extra = validate_module_tree([{"key": "c", "type": "columns", "limit": 3}])
    assert len(extra) == 1
    assert extra[0].startswith("[DATA_RULE_VIOLATION] modules[c]:")
    assert "limit" in extra[0]


def test_non_container_may_not_hold_children():
    problems = validate_module_tree([
        {"key": "r", "type": "progress_row", "children": [{"key": "x", "type": "note_block"}]},
    ])
    assert len(problems) == 1
    assert problems[0].startswith("[DATA_RULE_VIOLATION] modules[r].children:")


def test_every_problem_is_collected_not_just_the_first():
    problems = validate_module_tree([
        {"key": "a", "type": "nope"},
        {"key": "b", "type": "also_nope"},
        {"key": "c", "type": "progress_row", "params": "not-an-object"},
    ])
    assert len(problems) >= 3


# --------------------------------------------------------------------------- #
# Version locking
# --------------------------------------------------------------------------- #


def test_default_module_version_locks_the_major():
    assert default_module_version(get_module("progress_row")) == "^1.0"


@pytest.mark.parametrize(
    "requirement,version,expected",
    [
        (None, "1.0", True),
        ("", "1.0", True),
        ("^1.0", "1.0", True),
        ("^1.0", "1.4", True),
        ("^1.0", "2.0", False),
        # "~" locks major.minor and lets the patch grow (see module_version_matches).
        ("~1.0", "1.0.5", True),
        ("~1.0", "1.9", False),
        ("~1.9", "1.9.4", True),
        (">=1.0", "2.1", True),
        (">=1.2", "1.1", False),
        ("1.0", "1.0", True),
        ("1.0", "1.1", False),
        ("1", "1.7", True),
        ("1", "2.0", False),
        ("garbage", "1.0", False),
        ("^1.0", None, False),
    ],
)
def test_module_version_matches(requirement, version, expected):
    assert module_version_matches(requirement, version) is expected


def test_unsatisfiable_version_lock_is_rejected():
    problems = validate_module_tree([
        {"key": "r", "type": "progress_row", "module_version": "^2.0"},
    ])
    assert len(problems) == 1
    assert problems[0].startswith("[MODULE_NOT_WHITELISTED] modules[r]:")
    assert "does not satisfy module_version" in problems[0]


# --------------------------------------------------------------------------- #
# Param declarations: a declared range is enforced, a flat key is not ignored
# --------------------------------------------------------------------------- #


def test_table_declares_a_bounded_row_count():
    """`rows` is a declared param of `table`, 1-5, default 1 (拍板：上限 5)."""
    declaration = get_module("table").params["rows"]
    assert declaration["type"] == "int"
    assert declaration["required"] is False
    assert declaration["default"] == 1
    assert (declaration["minimum"], declaration["maximum"]) == (1, 5)


def test_module_params_reads_a_flat_declared_param():
    """`rows: 3` beside params.columns must be read, not silently dropped."""
    spec = get_module("table")
    flat = {"type": "table", "params": {"columns": []}, "rows": 3}
    assert module_params(flat)["rows"] == 3
    assert written_param_names(flat) == {"columns", "rows"}

    # Nested wins when both are present, and only declared names count as params.
    both = {"type": "table", "params": {"columns": [], "rows": 2}, "rows": 4}
    assert module_params(both)["rows"] == 2
    assert written_param_names({"type": "table", "rowz": 4}) == set()
    # Default applies when nobody wrote it.
    assert module_params({"type": "table", "params": {"columns": []}})["rows"] == 1
    assert spec.params["rows"]["default"] == 1


def test_param_problem_states_the_bound_it_broke():
    spec = get_module("table")
    assert param_problem(spec, "rows", 3) is None
    assert param_problem(spec, "rows", 1) is None
    assert param_problem(spec, "rows", 5) is None
    out_of_range = param_problem(spec, "rows", 9)
    assert out_of_range is not None and "at most 5" in out_of_range
    too_small = param_problem(spec, "rows", 0)
    assert too_small is not None and "at least 1" in too_small
    wrong_type = param_problem(spec, "rows", "3")
    assert wrong_type is not None and "must be an integer" in wrong_type
    assert param_problem(spec, "columns", []) is None
