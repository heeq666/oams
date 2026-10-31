"""数字的显示格式：一个数怎么写成给人看的样子。

这段原来住在 `funnel.py` 里，跟「十一个平台指标怎么取数、怎么推导」搅在一起。
商品数据中心下线之后，取数那一半随之消失，只剩格式化还有人用——`api._format_metric`
（部门 emit 指标）与 `render.aggregate`（模块里展示的聚合值）都调用它，好让页面上
同一个数在两处长得一样。所以把函数单独立一个文件。

**单位不在这里查表**：原来那张 `METRIC_UNITS` 的 11 个键全是 `funnel.*` 平台指标，
随商品中心下线一起删了；单位一律由调用方传进来（`unit` 参数），拿不到就传空串。
只放纯函数：不取数、不连库、不认识任何指标目录——别在这里加业务判断。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _as_int(value: float) -> int | float:
    """Counts and sums print as integers; keep a float only if it truly is one."""
    rounded = round(value)
    return int(rounded) if abs(value - rounded) < 1e-9 else float(value)


def _round_half_up(value: float, digits: int) -> Decimal:
    """Round a ratio the way a person reading 5.1751 expects: 5.18.

    Python's built-in round() is round-half-to-even on the decimal string, so
    round(5.175, 2) is 5.17. Going through Decimal keeps the half-up rule exact
    instead of inheriting binary float error.
    """
    quantum = Decimal(1).scaleb(-digits)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def format_metric(metric_key: str, value: float | int | None, unit: str = "") -> str:
    """Render an aggregate value for display.

    Shapes (contract §7)::

        63,289 件 / ¥1,745.87 万 / 5.18 / 59 款

    ``unit`` 由调用方给（`MetricSpec.unit`、模块参数或 binding 的 `unit`），
    **不在这张表里查**：这张表原来有 11 行 `funnel.*`，那批平台指标下线后全是
    死键，而部门指标 `dept.<CODE>.<slug>` 一个键都不在里面，将来中性演示数据
    （件/单/元/周）会全部无单位。空串是**合法**的显示结果——没有单位就不写单位，
    不要编一个。

    两条**形状**规则留在函数里，跟单位无关：`.gmv` 记成万元（`¥1,745.87 万`）、
    `.roi` 记两位小数。比例四舍五入（老板 2026-10-23 定：5.1751 显示 5.18，不是
    5.17），所以走 Decimal 而不是内置 round()。

    None renders as an em dash — "no denominator yet" must not look like "zero".
    """
    if value is None:
        return "—"
    if not isinstance(value, (int, float)):
        return str(value)
    key = metric_key or ""
    if key.endswith(".roi"):
        return f"{_round_half_up(float(value), 2):.2f}"
    number = f"{_as_int(float(value)):,}"
    if key.endswith(".gmv"):
        return f"¥{float(value) / 10000:,.2f} 万"
    return f"{number} {str(unit or '').strip()}".strip()
