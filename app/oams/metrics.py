"""Metric catalogue for department interfaces and the company home page (contract §2).

Two origins, and the difference is not cosmetic:

  * origin="platform"  -- the data centre computes these. A department may only
    *reference* them. The formula lives here so nobody can quietly redefine
    "已出样" to mean something more flattering.
  * origin="interface" -- the department declares its own metric over its own
    entry fields. Its key is forced into the `dept.<ORG_UNIT_CODE>.<slug>` namespace
    so an interface metric can never shadow a platform one, and an emitted metric
    can never be read by another department (concept §5.2).

Why a catalogue at all: a number on a screen without a stated, frozen 举口 is a rumour.
Every metric here carries `formula` (human-readable), `source` (which data centre
call feeds it) and `wave_scoped` -- the three things a reviewer needs to decide
whether a page is honest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oams.modules import STANDARD_STATUSES

#: Metric provenance.
ORIGINS: tuple[str, ...] = ("platform", "interface")

#: Home page trays (funnel stages).
GROUPS: tuple[str, ...] = ("design", "sample", "order", "money")

#: Allowed aggregation modes for an emitted (interface) metric.
AGG_MODES: tuple[str, ...] = ("count", "sum", "avg", "count_distinct")

#: Allowed binding modes.
BINDING_MODES: tuple[str, ...] = ("emit", "display")

#: Reject-reason codes (contract §3 / concept §4.4).
METRIC_UNDEFINED = "METRIC_UNDEFINED"
DATA_RULE_VIOLATION = "DATA_RULE_VIOLATION"
FIELD_UNMAPPED = "FIELD_UNMAPPED"

#: Prefix every interface metric key must carry when it is emitted.
DEPARTMENT_METRIC_PREFIX = "dept"


@dataclass(frozen=True)
class MetricSpec:
    key: str              # "funnel.sample_done"
    name: str             # "已出样"
    unit: str             # "款" | "件" | "元" | "倍" | "%" | ""
    origin: str           # "platform" | "interface"
    formula: str          # human-readable 口径
    source: str           # "datacenter:styles" etc.
    wave_scoped: bool
    group: str            # "design" | "sample" | "order" | "money"
    on_home: bool
    home_order: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "unit": self.unit,
            "origin": self.origin,
            "formula": self.formula,
            "source": self.source,
            "wave_scoped": self.wave_scoped,
            "group": self.group,
            "on_home": self.on_home,
            "home_order": self.home_order,
        }


def _metric(
    key: str,
    name: str,
    unit: str,
    formula: str,
    source: str,
    group: str,
    *,
    on_home: bool = False,
    home_order: int | None = None,
    wave_scoped: bool = True,
    origin: str = "platform",
) -> MetricSpec:
    if on_home and home_order is None:
        raise RuntimeError(f"metric {key!r}: on_home requires home_order")
    if not on_home and home_order is not None:
        raise RuntimeError(f"metric {key!r}: home_order requires on_home")
    return MetricSpec(
        key=key,
        name=name,
        unit=unit,
        origin=origin,
        formula=formula,
        source=source,
        wave_scoped=wave_scoped,
        group=group,
        on_home=on_home,
        home_order=home_order,
    )


# --------------------------------------------------------------------------- #
# platform metrics -- origin=platform, 11 of them, keys frozen by contract §2
# --------------------------------------------------------------------------- #

_PLATFORM: tuple[MetricSpec, ...] = (
    _metric(
        "funnel.designed", "有设计图", "款",
        "has_design_image = true", "datacenter:styles", "design",
    ),
    _metric(
        "funnel.sample_done", "已出样", "款",
        "至少一个颜色已出样（款级派生）", "datacenter:development", "sample",
        on_home=True, home_order=1,
    ),
    _metric(
        "funnel.sample_wip", "打样在制", "款",
        "无色出样且有色在制", "datacenter:development", "sample",
    ),
    _metric(
        "funnel.sample_pending", "打样未开始", "款",
        "无色出样、无色在制", "datacenter:development", "sample",
    ),
    _metric(
        "funnel.sample_open", "打样未完成", "款",
        "wip + pending", "datacenter:development", "sample",
        on_home=True, home_order=2,
    ),
    _metric(
        "funnel.priced", "核价完成", "款",
        "款级最新核价状态=核价完成", "datacenter:development", "sample",
    ),
    _metric(
        "funnel.ordered", "已下单", "款",
        "is_ordered = true", "datacenter:styles", "order",
        on_home=True, home_order=3,
    ),
    _metric(
        "funnel.not_ordered", "未下单", "款",
        "is_ordered = false", "datacenter:styles", "order",
        on_home=True, home_order=4,
    ),
    _metric(
        "funnel.qty", "下单件数", "件",
        "sum(quantity)", "datacenter:styles", "order",
    ),
    _metric(
        "funnel.gmv", "下单金额", "元",
        "sum(paid_amount)", "datacenter:styles", "money",
        on_home=True, home_order=5,
    ),
    _metric(
        "funnel.roi", "推广 ROI", "倍",
        "sum(marketing_sales)/sum(spend)", "datacenter:marketing", "money",
        on_home=True, home_order=6,
    ),
)


def _build_catalog(specs: tuple[MetricSpec, ...]) -> dict[str, MetricSpec]:
    catalog: dict[str, MetricSpec] = {}
    for spec in specs:
        if spec.key in catalog:
            raise RuntimeError(f"duplicate metric key in catalogue: {spec.key!r}")
        if spec.origin not in ORIGINS:
            raise RuntimeError(f"metric {spec.key!r}: unknown origin {spec.origin!r}")
        if spec.group not in GROUPS:
            raise RuntimeError(f"metric {spec.key!r}: unknown group {spec.group!r}")
        catalog[spec.key] = spec
    return catalog


METRIC_CATALOG: dict[str, MetricSpec] = _build_catalog(_PLATFORM)

#: The six cards the company home page shows by default, in display order.
HOME_DEFAULT: tuple[str, ...] = tuple(
    spec.key for spec in sorted(
        (spec for spec in METRIC_CATALOG.values() if spec.on_home),
        key=lambda spec: spec.home_order if spec.home_order is not None else 0,
    )
)


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def get_metric(key: str) -> MetricSpec | None:
    """Return the spec for `key`, or None when the metric is not defined."""
    if not isinstance(key, str):
        return None
    return METRIC_CATALOG.get(key)


def list_metrics(group: str | None = None, home_only: bool = False,
                 origin: str | None = None) -> list[dict[str, Any]]:
    """Metric specs as dicts, optionally filtered by group / home / origin."""
    if group not in (None, "", "all") and group not in GROUPS:
        raise ValueError(f"unknown metric group {group!r}; expected one of {GROUPS} or None")
    if origin not in (None, "", "all") and origin not in ORIGINS:
        raise ValueError(f"unknown metric origin {origin!r}; expected one of {ORIGINS} or None")

    specs = list(METRIC_CATALOG.values())
    if group not in (None, "", "all"):
        specs = [spec for spec in specs if spec.group == group]
    if origin not in (None, "", "all"):
        specs = [spec for spec in specs if spec.origin == origin]
    if home_only:
        specs = [spec for spec in specs if spec.on_home]
    return [spec.to_dict() for spec in specs]


# --------------------------------------------------------------------------- #
# Binding validation (descriptor `metrics[]` items)
# --------------------------------------------------------------------------- #


def department_metric_prefix(org_unit_code: str) -> str:
    """The namespace an interface metric must live in: `dept.<CODE>.`."""
    return f"{DEPARTMENT_METRIC_PREFIX}.{org_unit_code}."


def validate_metric_binding(binding: dict, org_unit_code: str,
                            fields: dict[str, dict]) -> list[str]:
    """Return every problem with one `metrics[]` binding, not just the first.

    emit    -- declares a department metric over this interface's own fields.
               Key must start with `dept.<org_unit_code>.`, `from_field` must be a
               declared field key, and it must not shadow a platform metric.
    display -- only references a platform metric, and carries no aggregation.
    """
    problems: list[str] = []
    if not isinstance(binding, dict):
        return [f"[{METRIC_UNDEFINED}] metrics: each binding must be an object"]

    key = binding.get("metric_key")
    if not isinstance(key, str) or not key.strip():
        return [f"[{METRIC_UNDEFINED}] metrics: metric_key is required"]
    key = key.strip()
    where = f"metrics[{key}]"

    mode = binding.get("mode")
    if mode not in BINDING_MODES:
        problems.append(
            f"[{METRIC_UNDEFINED}] {where}.mode: {mode!r} must be one of {BINDING_MODES}"
        )
        return problems

    known_metrics = get_metric(key)

    if mode == "display":
        if known_metrics is None or known_metrics.origin != "platform":
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.metric_key: display may only reference a platform "
                f"metric; {key!r} is not in the catalogue"
            )
        for forbidden in ("agg", "from_field", "where_status"):
            if forbidden in binding:
                problems.append(
                    f"[{DATA_RULE_VIOLATION}] {where}.{forbidden}: a display binding only "
                    f"references a platform metric and must not carry its own aggregation"
                )
        return problems

    # --- mode == "emit" ---------------------------------------------------- #
    prefix = department_metric_prefix(org_unit_code)
    if not key.startswith(prefix):
        problems.append(
            f"[{DATA_RULE_VIOLATION}] {where}.metric_key: an emitted metric must live in this "
            f"department's namespace, i.e. start with {prefix!r}"
        )
    elif len(key) == len(prefix):
        problems.append(
            f"[{METRIC_UNDEFINED}] {where}.metric_key: {key!r} has no slug after {prefix!r}"
        )
    if known_metrics is not None and known_metrics.origin == "platform":
        problems.append(
            f"[{DATA_RULE_VIOLATION}] {where}.metric_key: {key!r} is a platform metric and "
            f"cannot be redefined by a department"
        )

    agg = binding.get("agg")
    if agg not in AGG_MODES:
        problems.append(f"[{METRIC_UNDEFINED}] {where}.agg: {agg!r} must be one of {AGG_MODES}")

    from_field = binding.get("from_field")
    if not isinstance(from_field, str) or not from_field.strip():
        problems.append(f"[{FIELD_UNMAPPED}] {where}.from_field: an emitted metric must name a field")
    elif from_field not in fields:
        problems.append(
            f"[{FIELD_UNMAPPED}] {where}.from_field: {from_field!r} is not a declared field of "
            f"this interface"
        )

    if "where_status" in binding:
        status = binding.get("where_status")
        if status not in STANDARD_STATUSES:
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.where_status: {status!r} must be one of "
                f"{STANDARD_STATUSES}"
            )

    return problems
