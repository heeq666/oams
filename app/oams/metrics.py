"""接口指标的目录与绑定校验（契约 §2/§5.2）。

商品数据中心下线之后，这里只剩**一种**指标来源：`origin="interface"` —— 部门在自己
的页面字段上声明指标，键强制落在 `dept.<ORG_UNIT_CODE>.<slug>` 命名空间里，既不会
撞上别人的键，也不会被别的部门读到（概念文档 §5.2）。

平台指标（`funnel.*`，原来由外部商品数据中心计算）已随那次下线一起移除，`METRIC_CATALOG`
现在是空表；描述符里若还写着 `mode="display"` 去引用平台指标，会被 `METRIC_UNDEFINED`
可读地拒绝，而不是悄悄变成 0。emit 绑定不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oams.modules import STANDARD_STATUSES

#: Metric provenance. 只剩接口指标：平台指标随商品数据中心一起下线。
ORIGINS: tuple[str, ...] = ("interface",)

#: 首页托盘（曾用于平台漏斗的阶段分组）。平台指标下线后没有成员，保留常量是为了
#: 让 `list_metrics(group=...)` 对旧写法给出明确报错，而不是静默返回空列表。
GROUPS: tuple[str, ...] = ()

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


#: 目录现在是空的：平台指标移除后，接口指标是运行时按部门声明的，不进这张表。
METRIC_CATALOG: dict[str, MetricSpec] = _build_catalog(())


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
                            fields: dict[str, dict],
                            own_metrics: set[str] | None = None) -> list[str]:
    """Return every problem with one `metrics[]` binding, not just the first.

    emit    -- declares a department metric over this interface's own fields.
               Key must start with `dept.<org_unit_code>.`, `from_field` must be a
               declared field key, and it must not shadow a platform metric.
               可选 `label`（人话名）与 `unit`（单位）——名字与单位属声明方，见契约 §2/§7。
    display -- references a platform metric, **或本页自己 emit 的指标**
               （`own_metrics`，见契约 §2）；本身不带聚合字段，也**不带** `label`/`unit`。
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
        own = own_metrics if own_metrics is not None else set()
        prefix = department_metric_prefix(org_unit_code)
        if known_metrics is not None and known_metrics.origin == "platform":
            pass  # 目录里的平台指标：将来平台回来还能这么写
        elif key in own:
            pass  # 本页自己 emit、本页自己展示（自产自用）
        elif key.startswith(DEPARTMENT_METRIC_PREFIX + ".") and not key.startswith(prefix):
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.metric_key: {key!r} 属于别的部门的指标命名空间；"
                f"一页只能展示本部门（{prefix}）自己 emit 的指标"
            )
        elif key.startswith(prefix):
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.metric_key: 本页把 {key!r} 当展示指标用，"
                f"但它没有在 metrics[] 里以 mode=emit 声明（自产才能自用）"
            )
        else:
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.metric_key: {key!r} 既不是目录里的平台指标，"
                f"也不是本页 emit 的指标（后者须以 {prefix!r} 开头并在本页声明）"
            )
        for forbidden in ("agg", "from_field", "where_status"):
            if forbidden in binding:
                problems.append(
                    f"[{DATA_RULE_VIOLATION}] {where}.{forbidden}: a display binding only "
                    f"references a platform metric and must not carry its own aggregation"
                )
        # 名字与单位属**声明方**（emit 绑定，见契约 §2/§7）：引用方照着原样显示，
        # 不许自己改口径——否则同一页上同一个指标能有两套名字与两种单位。
        for owned in ("label", "unit"):
            if owned in binding:
                problems.append(
                    f"[{DATA_RULE_VIOLATION}] {where}.{owned}: 只有 mode=emit 的声明方"
                    f"能写 {owned}（人话名与单位跟着指标走）；display 是引用方，"
                    f"照原样显示即可，不能自带 {owned}"
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

    # 可选的两个显示字段（契约 §2/§7）：都属声明方。不给也能用——名字回落键末段，
    # 单位为空串（空串是合法的显示结果，不要编）。
    for field_name, limit, hint in (
        ("label", 24, "人话名，如「本周完成」"),
        ("unit", 8, "计量单位，如「项」「元」「件」"),
    ):
        if field_name not in binding:
            continue
        value = binding.get(field_name)
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.{field_name}: 必须是非空字符串（{hint}）；"
                f"不写这个字段等于没有，不要写成空串或 null"
            )
        elif len(value.strip()) > limit:
            problems.append(
                f"[{METRIC_UNDEFINED}] {where}.{field_name}: 最多 {limit} 个字（{hint}），"
                f"现在是 {len(value.strip())} 个"
            )

    return problems
