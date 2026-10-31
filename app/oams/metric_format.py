"""数字的显示格式：一个数怎么写成给人看的样子。

这段原来住在 `funnel.py` 里，跟「十一个平台指标怎么取数、怎么推导」搅在一起。
商品数据中心下线之后，取数那一半随之消失，只剩格式化还有人用——`api._format_metric`
（部门 emit 指标）与 `render.aggregate`（模块里展示的聚合值）都调用它，好让页面上
同一个数在两处长得一样。所以把函数连同它的单位表一起搬出来，单独立一个文件。

只放纯函数：不取数、不连库、不认识任何指标目录。单位表是**显示口径**（契约 §2/§7
的那几行），不是指标准入——别在这里加业务判断。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Display unit per metric key (contract §2 "unit" column). 钱用万元、比例保留两位。
METRIC_UNITS: dict[str, str] = {
    "funnel.designed": "款",
    "funnel.sample_done": "款",
    "funnel.sample_wip": "款",
    "funnel.sample_pending": "款",
    "funnel.sample_open": "款",
    "funnel.priced": "款",
    "funnel.ordered": "款",
    "funnel.not_ordered": "款",
    "funnel.qty": "件",
    "funnel.gmv": "元",
    "funnel.roi": "倍",
}


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


def format_metric(metric_key: str, value: float | int | None) -> str:
    """Render an aggregate value for display.

    Shapes (contract §7)::

        63,289 件 / ¥1,745.87 万 / 5.18 / 59 款

    None renders as an em dash — "no denominator yet" must not look like "zero".
    比例类指标保留两位小数并**四舍五入**（老板 2026-10-23 定：5.1751 显示 5.18，
    不是 5.17，所以这里走 Decimal 而不是内置 round()）。
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
    unit = METRIC_UNITS.get(key, "")
    return f"{number} {unit}".strip()
