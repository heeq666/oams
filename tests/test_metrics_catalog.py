"""Metric catalogue acceptance tests (contract §2, frozen 11 platform metrics).

The catalogue is the whole reason a department cannot invent its own number: the
platform metrics are frozen, the home page order is frozen, and a department may
only *emit* into its own namespace.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.metrics import (  # noqa: E402
    AGG_MODES,
    BINDING_MODES,
    DEPARTMENT_METRIC_PREFIX,
    GROUPS,
    HOME_DEFAULT,
    METRIC_CATALOG,
    ORIGINS,
    department_metric_prefix,
    get_metric,
    list_metrics,
    validate_metric_binding,
)

FROZEN_KEYS = (
    "funnel.designed",
    "funnel.sample_done",
    "funnel.sample_wip",
    "funnel.sample_pending",
    "funnel.sample_open",
    "funnel.priced",
    "funnel.ordered",
    "funnel.not_ordered",
    "funnel.qty",
    "funnel.gmv",
    "funnel.roi",
)


# --------------------------------------------------------------------------- #
# The frozen catalogue
# --------------------------------------------------------------------------- #


def test_catalogue_holds_exactly_the_eleven_platform_metrics():
    assert set(METRIC_CATALOG) == set(FROZEN_KEYS)
    assert len(METRIC_CATALOG) == 11
    assert ORIGINS == ("platform", "interface")
    assert all(spec.origin == "platform" for spec in METRIC_CATALOG.values())


def test_metric_groups_and_agg_modes_are_frozen():
    assert GROUPS == ("design", "sample", "order", "money")
    assert AGG_MODES == ("count", "sum", "avg", "count_distinct")
    assert BINDING_MODES == ("emit", "display")
    for spec in METRIC_CATALOG.values():
        assert spec.group in GROUPS, spec.key


def test_every_metric_carries_a_formula_and_a_source():
    for spec in METRIC_CATALOG.values():
        assert spec.formula.strip(), spec.key
        assert spec.source, spec.key
        assert spec.name.strip(), spec.key
        assert isinstance(spec.wave_scoped, bool), spec.key


def test_home_default_is_the_frozen_six_in_display_order():
    assert HOME_DEFAULT == (
        "funnel.sample_done",
        "funnel.sample_open",
        "funnel.ordered",
        "funnel.not_ordered",
        "funnel.gmv",
        "funnel.roi",
    )
    for order, key in enumerate(HOME_DEFAULT, start=1):
        assert METRIC_CATALOG[key].on_home is True
        assert METRIC_CATALOG[key].home_order == order


def test_off_home_metrics_have_no_home_order():
    for spec in METRIC_CATALOG.values():
        if spec.on_home:
            assert spec.home_order is not None, spec.key
        else:
            assert spec.home_order is None, spec.key
            assert spec.key not in HOME_DEFAULT


def test_to_dict_is_json_serialisable_and_side_effect_free():
    payload = list_metrics()
    json.dumps(payload)
    payload[0]["on_home"] = "tampered"
    assert isinstance(list_metrics()[0]["on_home"], bool)


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def test_get_metric():
    assert get_metric("funnel.gmv").name == "下单金额"
    assert get_metric("funnel.gmv").unit == "元"
    assert get_metric("nope") is None
    assert get_metric(None) is None


def test_list_metrics_filters():
    assert len(list_metrics()) == 11
    assert len(list_metrics(home_only=True)) == 6
    assert {m["key"] for m in list_metrics(home_only=True)} == set(HOME_DEFAULT)
    assert [m["key"] for m in list_metrics(group="sample")]
    assert {m["key"] for m in list_metrics(group="sample")} == {
        "funnel.sample_done", "funnel.sample_wip", "funnel.sample_pending",
        "funnel.sample_open", "funnel.priced",
    }
    assert {m["key"] for m in list_metrics(group="money")} == {"funnel.gmv", "funnel.roi"}
    assert len(list_metrics(origin="platform")) == 11
    assert list_metrics(origin="interface") == []
    with pytest.raises(ValueError):
        list_metrics(group="profit")


# --------------------------------------------------------------------------- #
# Binding validation (descriptor metrics[])
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fields():
    return {
        "stage": {"key": "stage", "map_to": "status"},
        "style_no": {"key": "style_no", "map_to": "title"},
        "qty": {"key": "qty", "map_to": "quantity"},
    }


def test_department_prefix():
    assert department_metric_prefix("PRODUCTION") == "dept.PRODUCTION."
    assert department_metric_prefix("PRODUCTION").startswith(DEPARTMENT_METRIC_PREFIX + ".")


def test_display_binding_accepts_a_platform_metric(fields):
    assert validate_metric_binding(
        {"metric_key": "funnel.sample_done", "mode": "display"}, "PRODUCTION", fields
    ) == []


def test_display_binding_rejects_an_unknown_or_interface_metric(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.blocked_count", "mode": "display"}, "PRODUCTION", fields
    )
    assert len(problems) == 1
    assert problems[0].startswith("[METRIC_UNDEFINED] metrics[dept.PRODUCTION.blocked_count].metric_key:")


def test_display_binding_must_not_carry_aggregation(fields):
    problems = validate_metric_binding(
        {"metric_key": "funnel.gmv", "mode": "display", "agg": "sum", "from_field": "qty"},
        "PRODUCTION", fields,
    )
    coded = {p.split("]")[0] + "]" for p in problems}
    assert coded == {"[DATA_RULE_VIOLATION]"}
    assert len(problems) == 2


def test_emit_binding_accepts_a_namespaced_metric(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.blocked_count", "mode": "emit",
         "agg": "count", "from_field": "stage", "where_status": "blocked"},
        "PRODUCTION", fields,
    )
    assert problems == []


def test_emit_binding_must_live_in_the_department_namespace(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.DESIGN.done", "mode": "emit", "agg": "count", "from_field": "stage"},
        "PRODUCTION", fields,
    )
    assert len(problems) == 1
    assert problems[0].startswith("[DATA_RULE_VIOLATION] metrics[dept.DESIGN.done].metric_key:")
    assert "dept.PRODUCTION." in problems[0]


def test_emit_binding_cannot_shadow_a_platform_metric(fields):
    problems = validate_metric_binding(
        {"metric_key": "funnel.gmv", "mode": "emit", "agg": "sum", "from_field": "qty"},
        "PRODUCTION", fields,
    )
    messages = " | ".join(problems)
    assert "namespace" in messages
    assert "is a platform metric and cannot be redefined" in messages


def test_emit_binding_requires_a_declared_field_and_known_agg(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.x", "mode": "emit", "agg": "median",
         "from_field": "not_a_field"},
        "PRODUCTION", fields,
    )
    assert len(problems) == 2
    assert any(p.startswith("[METRIC_UNDEFINED]") and ".agg:" in p for p in problems)
    assert any(p.startswith("[FIELD_UNMAPPED]") and ".from_field:" in p for p in problems)


def test_emit_binding_status_filter_must_be_a_standard_status(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.x", "mode": "emit", "agg": "count",
         "from_field": "stage", "where_status": "打样中"},
        "PRODUCTION", fields,
    )
    assert len(problems) == 1
    assert problems[0].startswith("[METRIC_UNDEFINED]")
    assert ".where_status:" in problems[0]


def test_binding_mode_and_shape_errors():
    assert validate_metric_binding("nope", "PRODUCTION", {}) == [
        "[METRIC_UNDEFINED] metrics: each binding must be an object"
    ]
    assert validate_metric_binding({}, "PRODUCTION", {}) == [
        "[METRIC_UNDEFINED] metrics: metric_key is required"
    ]
    bad_mode = validate_metric_binding(
        {"metric_key": "funnel.gmv", "mode": "publish"}, "PRODUCTION", {}
    )
    assert len(bad_mode) == 1
    assert ".mode:" in bad_mode[0]
