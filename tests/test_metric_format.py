"""`format_metric` 的数字形状（原 `tests/test_funnel.py` 的 format_metric 段）。

这几个断言不属于商品数据中心那条竖切：`api._format_metric`（部门 emit 指标）与
`render.aggregate`（模块里的聚合值）都在用同一个格式化入口，所以商品中心拆掉后它们必须留着。

**单位由调用方传**：`METRIC_UNITS` 那张表的 11 个键全是已删的平台指标，随商品中心一起
删掉了；`format_metric(key, value, unit="")` 不再查表，空单位只输出数字（合法结果）。
所以下面测的是「形状」，不是「某张已删的键表」。

Run: .venv/bin/python -m pytest tests/test_metric_format.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.metric_format import format_metric  # noqa: E402


def test_format_metric_uses_the_contract_shapes():
    # 单位显式传入：接口指标的单位来自 MetricSpec.unit / binding 的 unit，不查表。
    assert format_metric("dept.PRODUCTION.qty", 63289, "件") == "63,289 件"
    assert format_metric("dept.PRODUCTION.gmv", 17458700) == "¥1,745.87 万"
    assert format_metric("dept.PRODUCTION.roi", 5.1751) == "5.18"
    assert format_metric("dept.PRODUCTION.weeklies", 59, "周") == "59 周"


def test_format_metric_renders_none_as_dash():
    assert format_metric("dept.PRODUCTION.roi", None) == "—"
    assert format_metric("dept.PRODUCTION.gmv", None) == "—"
    # 空值连单位都不该出现："还没有分母" 不能长得像 "零"。
    assert format_metric("dept.PRODUCTION.qty", None, "件") == "—"


def test_format_metric_without_a_unit_prints_the_number_alone():
    # 空单位是合法结果（键查不到单位时就是它），不是缺陷；别为了「好看」编一个单位。
    assert format_metric("dept.PRODUCTION.blocked_count", 3176) == "3,176"
    assert format_metric("dept.PRODUCTION.x", 0, "") == "0"


def test_format_metric_roi_rounds_half_up():
    # Owner ruling 2026-10-23: ROI 四舍五入. 6889366 / 1331248 = 5.17511... -> 5.18.
    assert format_metric("dept.PRODUCTION.roi", 6889366 / 1331248) == "5.18"


def test_roi_rounding_is_half_up_at_an_exact_half():
    # The reason this is Decimal and not round(): round(5.175, 2) is 5.17 under
    # banker's rounding, which is the opposite of what was ruled.
    assert round(5.175, 2) == 5.17
    assert format_metric("dept.PRODUCTION.roi", 5.175) == "5.18"
    assert format_metric("dept.PRODUCTION.roi", 5.165) == "5.17"
    assert format_metric("dept.PRODUCTION.roi", 5.174) == "5.17"
    assert format_metric("dept.PRODUCTION.roi", 5.176) == "5.18"


def test_roi_rounding_does_not_inherit_binary_float_error():
    # 1.005 is 1.00499999999999989 in binary; str() keeps the decimal the caller
    # meant, so an exact half still goes up.
    assert format_metric("dept.PRODUCTION.roi", 1.005) == "1.01"


def test_format_metric_groups_thousands():
    assert format_metric("dept.PRODUCTION.qty", 3176, "件") == "3,176 件"
    assert format_metric("dept.PRODUCTION.gmv", 12345678) == "¥1,234.57 万"
