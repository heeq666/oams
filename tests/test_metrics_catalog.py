"""Metric catalogue acceptance tests (contract §2).

平台指标（11 条 `funnel.*`）随商品中心下线一起删掉了，目录现在是**空表**：
接口指标由部门在自己的描述符里 `emit`，运行时按部门解析，不进这张冻结表。

所以这里断言两件事，别把它们混起来：

1. **目录确实空了**，过滤接口还活着（`group` / `origin` 传不认识的值要抛错）；
2. **binding 校验的机制还在**——display 只能引用平台指标（现在一个都没有，所以
   任何 display 都是可读拒绝），emit 必须落在本部门命名空间。对象换了，机制没换。

⚠️ 空集合上循环会「0 次全绿」：凡是「遍历目录里每条 spec 断言什么」的用例都已经删掉，
换成对着直接构造的 `MetricSpec` 断言，别退回去。
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
    METRIC_CATALOG,
    METRIC_UNDEFINED,
    ORIGINS,
    MetricSpec,
    _build_catalog,
    department_metric_prefix,
    get_metric,
    list_metrics,
    validate_metric_binding,
)


# --------------------------------------------------------------------------- #
# The catalogue is empty now
# --------------------------------------------------------------------------- #


def test_catalogue_is_empty_after_the_product_hub_removal():
    assert METRIC_CATALOG == {}
    assert ORIGINS == ("interface",)
    assert GROUPS == ()


def test_agg_modes_and_binding_modes_are_frozen():
    assert AGG_MODES == ("count", "sum", "avg", "count_distinct")
    assert BINDING_MODES == ("emit", "display")


def test_list_metrics_returns_nothing_and_still_validates_its_filters():
    assert list_metrics() == []
    assert list_metrics(origin="interface") == []
    assert list_metrics(home_only=True) == []
    assert list_metrics(group="all") == []
    # 过滤参数本身仍然要校验：拼错分组不能静默返回空表。
    with pytest.raises(ValueError):
        list_metrics(group="profit")
    with pytest.raises(ValueError):
        list_metrics(origin="platform")


def test_get_metric():
    # 平台指标已删，键查不到就是 None（不是抛错）。
    assert get_metric("funnel.gmv") is None
    assert get_metric("nope") is None
    assert get_metric(None) is None
    assert get_metric(123) is None


# --------------------------------------------------------------------------- #
# MetricSpec 自己（不依赖目录里有什么）
# --------------------------------------------------------------------------- #


def _interface_spec() -> MetricSpec:
    return MetricSpec(
        key="dept.OPS.weeklies",
        name="提交周报数",
        unit="份",
        origin="interface",
        formula="count(entries where kind='weekly')",
        source="interface:entries",
        wave_scoped=False,
        group="",
        on_home=False,
        home_order=None,
    )


def test_metric_spec_carries_a_formula_and_a_source():
    spec = _interface_spec()
    assert spec.formula.strip()
    assert spec.source.strip()
    assert spec.name.strip()
    assert isinstance(spec.wave_scoped, bool)


def test_to_dict_is_json_serialisable_and_side_effect_free():
    spec = _interface_spec()
    payload = spec.to_dict()
    json.dumps(payload)
    payload["on_home"] = "tampered"
    payload["key"] = "tampered"
    # 每次都从 spec 现取：改返回的 dict 不能污染 spec 本身。
    assert spec.to_dict()["on_home"] is False
    assert spec.to_dict()["key"] == "dept.OPS.weeklies"


def test_catalog_builder_still_rejects_unknown_origins_and_duplicates():
    # 目录空着，但建目录时的校验不能跟着一起变空——它挡的是「有人偷偷加回平台指标」。
    with pytest.raises(RuntimeError):
        _build_catalog((_interface_spec(),))  # group "" 不在 GROUPS 里
    platform_ish = MetricSpec(
        key="funnel.gmv", name="下单金额", unit="元", origin="platform",
        formula="sum", source="datacenter", wave_scoped=True, group="",
        on_home=False, home_order=None,
    )
    with pytest.raises(RuntimeError):
        _build_catalog((platform_ish,))


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


def test_display_binding_rejects_a_removed_platform_metric(fields):
    # 机制：display 只能引用平台指标，而平台指标一个不剩 —— 所以必须是**可读拒绝**，
    # 不是崩，也不是放行。
    problems = validate_metric_binding(
        {"metric_key": "funnel.sample_done", "mode": "display"}, "PRODUCTION", fields
    )
    assert len(problems) == 1
    assert problems[0].startswith(f"[{METRIC_UNDEFINED}] metrics[funnel.sample_done].metric_key:")
    assert "display may only reference a platform metric" in problems[0]
    assert "is not in the catalogue" in problems[0]


def test_display_binding_rejects_an_unknown_or_interface_metric(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.blocked_count", "mode": "display"}, "PRODUCTION", fields
    )
    assert len(problems) == 1
    assert problems[0].startswith("[METRIC_UNDEFINED] metrics[dept.PRODUCTION.blocked_count].metric_key:")


def test_display_binding_must_not_carry_aggregation(fields):
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.qty", "mode": "display", "agg": "sum", "from_field": "qty"},
        "PRODUCTION", fields,
    )
    # 带了聚合就是数据规则问题（键本身未定义是另一条，两条各自独立报）。
    assert sum(1 for p in problems if p.startswith("[DATA_RULE_VIOLATION]")) == 2


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
        {"metric_key": "dept.PRODUCTION.x", "mode": "publish"}, "PRODUCTION", {}
    )
    assert len(bad_mode) == 1
    assert ".mode:" in bad_mode[0]
