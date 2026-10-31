"""The eleven platform metrics on the company home page (contract §2/§7).

This module owns exactly one question: "given the product data centre, what are
the numbers?" It never talks to the network itself (that is
oams.datacenter) and never touches the database, so every function here is a
pure function of its inputs and can be tested without either.

The one rule that is easy to get wrong and expensive to get wrong
------------------------------------------------------------------
sample_status is a **colour** level status: one style has one development record
per colour, and a style's record therefore carries a list of colour states. The
style level "已出样" is **derived** — at least one colour has been sampled — and
is never read from a hand-filled style-level field. See
docs/architecture/plugin-host-concept.md 契约三: if a department could fill the
style level themselves, the same style would have two answers in two places.

Derivation priority (concept doc line 137 / 146-150):

    done    至少一个颜色是 已出样
    wip     没有颜色出样，但有颜色在制（配料中 / 制版中 / 工艺中 / 制衣中 ...）
    pending 其余（全为 未开始 / 未填写，或根本没有开发记录）

A style with no development record at all is "pending" by absence, which is why
the three buckets always add up to the style count.
"""
from __future__ import annotations


from oams.datacenter import DataCenter, as_bool
from oams.metric_format import METRIC_UNITS, _as_int, _round_half_up, format_metric  # noqa: F401  格式化实现在那边

#: Colour-level words that mean "this colour has been sampled".
SAMPLE_DONE_WORDS: tuple[str, ...] = ("已出样",)

#: Colour-level words that mean "this colour is being worked on, not sampled yet".
#:
#: Ordered along the production flow: 配料 -> 剪裁 -> 工艺 -> 制衣. The four words
#: named by the concept doc (配料中 / 制版中 / 工艺中 / 制衣中) come first; the rest
#: are the neighbouring stages of the same colour vocabulary the data centre
#: really emits (docs/runbooks/product-hub-data-access.md lists all 21 values),
#: so a colour sitting in 前工艺中 or 配料完成-待剪裁 is not silently counted as
#: "not started".
#:
#: The last three are not a stage on that line but were ruled to belong here by
#: the product owner (2026-10-23): 重做 restarts a colour that already went round
#: once, and 已安排 / 毛衣已安排 is booked with the factory. Work that has been
#: booked or re-booked is in production, so filing it under "not started" would
#: overstate 打样未完成. 后工艺 and 剪裁中 were checked against the owner's list
#: and were already here.
SAMPLE_WIP_WORDS: tuple[str, ...] = (
    "配料中",
    "制版中",
    "工艺中",
    "制衣中",
    "前工艺中",
    "后工艺",
    "配料完成",
    "剪裁中",
    "待剪裁",
    "配料完成-待剪裁",
    "重做",
    "已安排",
    "毛衣已安排",
)

#: Colour words this module deliberately does **not** classify as in production.
#: They are terminal-negative (取消 / 失败), genuinely untouched (未开始 /
#: 未填写 / 待定), or metadata (AI效果图 / ODM / 特殊情况).
#: A style whose colours are all in this set derives to "pending", the
#: conservative reading: it cannot be sold and it is not visibly in production.
#:
#: 重做 / 已安排 / 毛衣已安排 used to sit here and moved to SAMPLE_WIP_WORDS by
#: owner ruling (2026-10-23). What is left is the residue that really is "no
#: work started", plus words that describe a colour rather than a stage.
UNCLASSIFIED_SAMPLE_WORDS: tuple[str, ...] = (
    "未开始",
    "未填写",
    "取消",
    "ODM",
    "失败",
    "特殊情况",
    "待定",
    "AI效果图",
)

#: Style-level pricing status that counts towards funnel.priced.
PRICED_DONE_WORD = "核价完成"

#: The eleven platform metric keys, in contract §2 order.
FUNNEL_KEYS: tuple[str, ...] = (
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

_DONE = frozenset(SAMPLE_DONE_WORDS)
_WIP = frozenset(SAMPLE_WIP_WORDS)


def derive_style_sample_state(color_states: list[str]) -> str:
    """Fold a style's colour-level states into one style-level state.

    Args:
        color_states: the colour-level sample_status words of one style. Order
            and duplicates are irrelevant; unknown words are ignored.

    Returns:
        "done" if any colour was sampled, else "wip" if any colour is in
        production, else "pending".
    """
    words = {str(state).strip() for state in color_states or [] if state}
    if words & _DONE:
        return "done"
    if words & _WIP:
        return "wip"
    return "pending"


def _as_int(value: float) -> int | float:
    """Counts and sums print as integers; keep a float only if it truly is one."""
    rounded = round(value)
    return int(rounded) if abs(value - rounded) < 1e-9 else float(value)


def compute_funnel(dc: DataCenter, *, wave: str | None = None) -> dict[str, float | int]:
    """Compute the eleven platform metrics for one wave (or for all waves).

    Args:
        dc: the read-only data centre.
        wave: a wave id such as "26D冬1-2波", or None for every wave.

    Returns:
        A dict keyed by the contract's metric keys. funnel.roi is None when no
        marketing spend was recorded, because a ratio with no denominator is
        undefined rather than zero.
    """
    products = dc.list_products(wave=wave)
    development = dc.list_development(wave=wave) or {}
    marketing = dc.list_marketing(wave=wave) or {}

    styles: dict[str, dict] = development.get("styles") or {}

    designed = 0
    ordered = 0
    quantity = 0.0
    gmv = 0.0
    sample = {"done": 0, "wip": 0, "pending": 0}
    priced = 0

    for product in products:
        code = product.get("style_code")
        if as_bool(product.get("has_design_image")):
            designed += 1

        # Only the boolean decides "已下单". order_status is never consulted:
        # the API ignores it (every value returns the full set), so using it
        # would silently turn 29/39 into 3,176/0.
        if as_bool(product.get("is_ordered")):
            ordered += 1
            quantity += float(product.get("quantity") or 0)
            gmv += float(product.get("paid_amount") or 0)

        entry = styles.get(code) or {}
        sample[derive_style_sample_state(entry.get("sample_states") or [])] += 1

        # list_development already resolved the *latest* version per style.
        status = (entry.get("pricing_status") or "").strip()
        if status == PRICED_DONE_WORD:
            priced += 1

    totals = marketing.get("totals") or {}
    spend = float(totals.get("spend") or 0)
    sales = float(totals.get("marketing_sales") or 0)
    roi = (sales / spend) if spend else None

    total = len(products)
    return {
        "funnel.designed": designed,
        "funnel.sample_done": sample["done"],
        "funnel.sample_wip": sample["wip"],
        "funnel.sample_pending": sample["pending"],
        "funnel.sample_open": sample["wip"] + sample["pending"],
        "funnel.priced": priced,
        "funnel.ordered": ordered,
        "funnel.not_ordered": total - ordered,
        "funnel.qty": _as_int(quantity),
        "funnel.gmv": _as_int(gmv),
        "funnel.roi": roi,
    }
