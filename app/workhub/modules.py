"""Preset module registry for department interfaces (P0 contract §1).

Why this file exists: a department interface is data, not code. Everything it can
draw comes from this table, so the host can render it, diff it, and refuse it
without ever executing anything. The registry is the single place where a module
declares what it *consumes* (standard fields it reads), what it *produces*
(standard fields the company aggregate may trust), and whether it can nest.

Layer rules (§1 / concept §3.1):
  * container -- layout only. No data of its own: no source_fields, no literal
    text (params.text), nothing but children. Can nest, at most
    CONTAINER_DEPTH_LIMIT levels deep (concept §3.3: two levels, no more).
  * input     -- the only layer that produces entries and standard fields.
  * display   -- read-only views of already-computed metrics; produces nothing,
    aggregatable is None (not applicable, not "no").

The table below is frozen by docs/architecture/p0-interface-contract.md §1.
Keys may not be renamed; adding a module means adding a row to the contract first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Shared vocabularies (frozen). They live here because both the module table and
# the descriptor validator need them and neither may import the other in a cycle.
# --------------------------------------------------------------------------- #

LAYERS: tuple[str, ...] = ("container", "input", "display")

#: The four company-wide statuses. Every department status word must land on one.
STANDARD_STATUSES: tuple[str, ...] = ("planned", "in_progress", "blocked", "done")

#: Standard field vocabulary, frozen from the product data centre (contract §5).
#: `map_to` may only name one of these, or the literal "display_only".
STANDARD_FIELD_VOCAB: tuple[str, ...] = (
    # A. object references
    "style_code", "skc_code", "color", "sku_code", "wave", "category", "brand",
    "designer", "style_position", "supplier_code", "material_code", "order_no",
    # B. measures
    "count", "quantity", "amount", "percent", "ratio", "duration", "metric_value",
    # C. time
    "planned_at", "due_at", "completed_at", "reported_at", "status_since",
    # D. status
    # NOTE: sample_status / approval_status are *canonical names*, not *field
    # mappings*. They stay in the vocabulary because the vocabulary is the shared
    # language of the interface contract and the product data centre writes these
    # two itself (style-level status is derived from colour-level development
    # records). A department descriptor may NOT use them as map_to -- the
    # validator rejects that with DATA_RULE_VIOLATION (see
    # workhub.descriptor.PLATFORM_DERIVED_FIELDS). Read this line as "the platform
    # can say sample_status", not "a department can map a field to sample_status".
    "status", "sample_status", "approval_status",
    # E. text and evidence
    "title", "note", "evidence", "evidence_url", "file",
    # F. relations
    "work_item_id", "parent_ref",
    # G. 汇报过程字段 (concept line 80: the process columns a report needs)
    "blocked_reason", "needs_decision", "progress_value",
)

#: Reject-reason codes this file can raise (contract §3 / concept §4.4).
MODULE_NOT_WHITELISTED = "MODULE_NOT_WHITELISTED"
LAYOUT_TOO_DEEP = "LAYOUT_TOO_DEEP"
DATA_RULE_VIOLATION = "DATA_RULE_VIOLATION"

#: Containers may nest at most this many layout levels (contract §1, concept §3.3).
CONTAINER_DEPTH_LIMIT = 2

#: A container node may carry identity, ordering, version and children -- nothing else.
CONTAINER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"key", "type", "order", "children", "params", "module_version"}
)

#: Keys that only a v0.2 descriptor may use (contract §3).
MODULES_V02_KEYS: tuple[str, ...] = ("children", "module_version")


# --------------------------------------------------------------------------- #
# The spec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModuleSpec:
    key: str                   # "progress_row"
    name: str                  # "进度行"
    layer: str                 # "container" | "input" | "display"
    version: str               # "1.0"
    description: str
    params: dict               # param declaration: name -> {type, required, default}
    consumes: tuple[str, ...]  # accepted source_fields (standard field names); () = none
    produces: tuple[str, ...]  # produced standard fields; () = none
    aggregatable: bool | None  # True / False; None = not applicable (container, display)
    can_nest: bool             # only containers may be True

    def to_dict(self) -> dict[str, Any]:
        """The shape MCP `list_modules` returns (JSON-serialisable, deep-copied)."""
        return {
            "key": self.key,
            "name": self.name,
            "layer": self.layer,
            "version": self.version,
            "description": self.description,
            "params": {name: dict(spec) for name, spec in self.params.items()},
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "aggregatable": self.aggregatable,
            "can_nest": self.can_nest,
        }


def _param(type_name: str, *, default: Any = None, required: bool = False) -> dict[str, Any]:
    return {"type": type_name, "required": required, "default": default}


def _spec(
    key: str,
    name: str,
    layer: str,
    description: str,
    params: dict[str, dict[str, Any]] | None = None,
    consumes: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    aggregatable: bool | None = None,
    version: str = "1.0",
) -> ModuleSpec:
    return ModuleSpec(
        key=key,
        name=name,
        layer=layer,
        version=version,
        description=description,
        params=dict(params or {}),
        consumes=tuple(consumes),
        produces=tuple(produces),
        aggregatable=aggregatable,
        can_nest=(layer == "container"),
    )


# --------------------------------------------------------------------------- #
# The registry (20 modules, keys frozen by contract §1)
# --------------------------------------------------------------------------- #

MODULE_REGISTRY: dict[str, ModuleSpec] = {}


def _register(*specs: ModuleSpec) -> None:
    for spec in specs:
        if spec.key in MODULE_REGISTRY:
            raise RuntimeError(f"duplicate module key in registry: {spec.key!r}")
        if spec.layer not in LAYERS:
            raise RuntimeError(f"module {spec.key!r}: unknown layer {spec.layer!r}")
        if spec.layer == "container" and not spec.can_nest:
            raise RuntimeError(f"module {spec.key!r}: containers must be able to nest")
        if spec.layer != "container" and spec.can_nest:
            raise RuntimeError(f"module {spec.key!r}: only containers may nest")
        if spec.layer == "display" and (spec.consumes or spec.produces):
            raise RuntimeError(f"module {spec.key!r}: display modules consume aggregates, not fields")
        MODULE_REGISTRY[spec.key] = spec


# --- containers ------------------------------------------------------------
_register(
    _spec(
        "columns", "分栏", "container",
        "把子模块按固定列数并列排列。",
        {"columns": _param("int", default=2), "gap": _param("str", default="md")},
    ),
    _spec(
        "rows", "行", "container",
        "把子模块从上到下堆叠。",
        {"gap": _param("str", default="md")},
    ),
    _spec(
        "grid", "网格", "container",
        "等宽网格布局，适合一组卡片。",
        {"columns": _param("int", default=3), "gap": _param("str", default="md")},
    ),
    _spec(
        "card", "卡片", "container",
        "带标题的容器，标题只能来自参数。",
        {"title": _param("str", required=True)},
    ),
    _spec(
        "tabs", "标签页", "container",
        "按标签切换子模块。",
        {"labels": _param("list[str]", required=True)},
    ),
    _spec(
        "divider", "分隔线", "container",
        "可选文字的分隔线。",
        {"label": _param("str", default="")},
    ),
    _spec(
        "spacer", "留白", "container",
        "空隔，不承载任何数据。",
        {"size": _param("str", default="md")},
    ),
)

# --- input modules ---------------------------------------------------------
_register(
    _spec(
        "progress_row", "进度行", "input",
        "一件事的推进：标题、状态、进度值、备注、依据。",
        consumes=("title", "status", "progress_value", "note", "evidence"),
        produces=("status", "progress_value", "note", "evidence"),
        aggregatable=True,
    ),
    _spec(
        "table", "明细表", "input",
        "多行明细，列由参数声明。",
        {"columns": _param("list[{key,label}]", required=True)},
        consumes=("title", "status", "note"),
        produces=("status", "note"),
        aggregatable=True,
    ),
    _spec(
        "timeline", "里程碑", "input",
        "按到期时间排序的里程碑。",
        consumes=("title", "due_at", "status"),
        produces=("due_at", "status"),
        aggregatable=True,
    ),
    _spec(
        "blocker_list", "阻塞清单", "input",
        "阻塞项与需要决策的事项。",
        consumes=("title", "blocked_reason", "needs_decision", "status"),
        produces=("blocked_reason", "needs_decision"),
        aggregatable=True,
    ),
    _spec(
        "metric_input", "数值录入", "input",
        "一个带单位的数值，进入部门汇总。",
        {"unit": _param("str", required=True)},
        consumes=("title", "metric_value"),
        produces=("metric_value",),
        aggregatable=True,
    ),
    _spec(
        "evidence_gallery", "证据区", "input",
        "图片/附件证据，不进公司汇总。",
        consumes=("title", "evidence_url", "note"),
        produces=("evidence_url",),
        aggregatable=False,
    ),
)

# --- display modules -------------------------------------------------------
_register(
    _spec(
        "metric_card", "指标卡", "display",
        "一个平台指标的真实数字。",
        {"metric_key": _param("str", required=True), "span": _param("int", default=1)},
    ),
    _spec(
        "metric_grid", "指标网格", "display",
        "一组平台指标的并排展示。",
        {"metric_keys": _param("list[str]", required=True), "columns": _param("int", default=3)},
    ),
    _spec(
        "flow_strip", "流程条", "display",
        "流程各段的计数，标签由参数给出。",
        {"metrics": _param("list[str]", required=True), "labels": _param("list[str]", required=True)},
    ),
    _spec(
        "status_chart", "状态分布", "display",
        "按四态聚合的分布图。",
        {"dimension": _param("str", default="status")},
    ),
    _spec(
        "rank_list", "排行榜", "display",
        "按一个平台指标排序的 TOP N。",
        {"metric_key": _param("str", required=True), "limit": _param("int", default=5)},
    ),
    _spec(
        "activity_feed", "动态流", "display",
        "按上报时间倒序的活动记录。",
        {"limit": _param("int", default=10)},
    ),
    _spec(
        "note_block", "说明块", "display",
        "纯文字说明，文字由参数给出。",
        {"text": _param("str", required=True)},
    ),
)


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #


def get_module(key: str) -> ModuleSpec | None:
    """Return the spec for `key`, or None when the key is not registered."""
    if not isinstance(key, str):
        return None
    return MODULE_REGISTRY.get(key)


def list_modules(layer: str | None = None) -> list[dict[str, Any]]:
    """All module specs as dicts, in registry order; `layer` filters one layer."""
    specs = list(MODULE_REGISTRY.values())
    if layer not in (None, "", "all"):
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}; expected one of {LAYERS} or None")
        specs = [spec for spec in specs if spec.layer == layer]
    return [spec.to_dict() for spec in specs]


def _container_level(nodes: Any, level: int) -> int:
    """Deepest *container* level reachable from `nodes`.

    Contract §1 depth semantics: leaves do not count. The module list itself is
    level 0, and entering a container node adds one. So
    `columns > card > progress_row` peaks at 2 (columns=1, card=2, the leaf adds
    nothing) and is legal, while `columns > card > grid > ...` reaches 3 and is not.
    """
    if not isinstance(nodes, list):
        return level
    deepest = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        spec = get_module(node.get("type"))
        if spec is not None and spec.layer == "container":
            here = level + 1
            deepest = max(deepest, here, _container_level(node.get("children"), here))
        else:
            # Leaves neither add a level nor change the parent's level, but their
            # children (illegal, reported separately) still get walked.
            deepest = max(deepest, _container_level(node.get("children"), level))
    return deepest


def module_depth(modules: list[dict]) -> int:
    """Layout nesting depth, counted in *containers* (contract §1).

    0 when the layout holds no container, 1 for one container level (whether or
    not it wraps leaves), 2 for a container inside a container. Leaves never count.
    """
    if not isinstance(modules, list) or not modules:
        return 0
    return _container_level(modules, 0)


# --------------------------------------------------------------------------- #
# Module version locking (concept §3.4)
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


def _parse_version(text: Any) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0))


def default_module_version(spec: ModuleSpec) -> str:
    """The requirement implied when `module_version` is absent: lock the major."""
    return f"^{spec.version}"


def module_version_matches(requirement: Any, version: Any) -> bool:
    """Does `version` satisfy the locked `requirement`?

    Supported forms: "^1.0" (major locked, minor may grow), "~1.0" (major.minor
    locked, patch may grow), ">=1.0", "1.0" (exact), "1" (major only).
    An empty/absent requirement always matches.
    """
    if requirement is None or (isinstance(requirement, str) and not requirement.strip()):
        return True
    if not isinstance(requirement, str):
        return False
    raw = requirement.strip()
    operator = ""
    for candidate in ("^", "~", ">=", "==", "="):
        if raw.startswith(candidate):
            operator, raw = candidate, raw[len(candidate):].strip()
            break
    locked = _parse_version(raw)
    actual = _parse_version(version)
    if locked is None or actual is None:
        return False
    if operator == "^":
        return actual[0] == locked[0] and actual >= locked
    if operator == "~":
        return actual[:2] == locked[:2] and actual >= locked
    if operator == ">=":
        return actual >= locked
    if operator == "" and "." not in raw:
        return actual[0] == locked[0]
    return actual == locked


# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #


def _label(node: dict, index: int, prefix: str) -> str:
    key = node.get("key")
    name = key if isinstance(key, str) and key else f"#{index}"
    return f"{prefix}[{name}]"


def validate_module_tree(modules: list[dict]) -> list[str]:
    """Return every structural problem in the layout, not just the first one.

    Blocked here (contract §1 "铁律" + concept §3):
      * a type that is not in MODULE_REGISTRY               -> MODULE_NOT_WHITELISTED
      * a module_version the registry cannot satisfy        -> MODULE_NOT_WHITELISTED
      * containers declaring source_fields / params.text /
        any key beyond children+identity                    -> DATA_RULE_VIOLATION
      * non-containers carrying children                    -> DATA_RULE_VIOLATION
      * container nesting deeper than CONTAINER_DEPTH_LIMIT -> LAYOUT_TOO_DEEP
        (counted in containers: root container = 1, leaves add nothing)
    """
    problems: list[str] = []
    if not isinstance(modules, list):
        return [f"[{MODULE_NOT_WHITELISTED}] modules: must be a list of module objects"]
    if not modules:
        return [f"[{MODULE_NOT_WHITELISTED}] modules: must contain at least one module"]

    def walk(nodes: list[Any], prefix: str, container_level: int) -> None:
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                problems.append(
                    f"[{MODULE_NOT_WHITELISTED}] {prefix}[{index}]: module must be an object"
                )
                continue
            path = _label(node, index, prefix)
            mtype = node.get("type")
            spec = get_module(mtype)

            if spec is None:
                if mtype == "custom":
                    problems.append(
                        f"[{MODULE_NOT_WHITELISTED}] {path}: type 'custom' was retired in "
                        f"schema 0.2; compose registered containers instead"
                    )
                elif mtype in MODULE_REGISTRY:
                    problems.append(f"[{MODULE_NOT_WHITELISTED}] {path}: type {mtype!r} is malformed")
                else:
                    problems.append(
                        f"[{MODULE_NOT_WHITELISTED}] {path}: type {mtype!r} is not a registered "
                        f"module; registered types are {sorted(MODULE_REGISTRY)}"
                    )
                continue

            # Only containers add a level (contract §1: leaves do not count).
            level = container_level + 1 if spec.layer == "container" else container_level
            if spec.layer == "container" and level > CONTAINER_DEPTH_LIMIT:
                problems.append(
                    f"[{LAYOUT_TOO_DEEP}] {path}: container level {level} exceeds the limit of "
                    f"{CONTAINER_DEPTH_LIMIT}; flatten the layout or split the interface"
                )

            requirement = node.get("module_version")
            if not module_version_matches(requirement, spec.version):
                problems.append(
                    f"[{MODULE_NOT_WHITELISTED}] {path}: {spec.key}@{spec.version} does not "
                    f"satisfy module_version {requirement!r}"
                )

            params = node.get("params")
            if params is not None and not isinstance(params, dict):
                problems.append(
                    f"[{DATA_RULE_VIOLATION}] {path}.params: must be an object, got "
                    f"{type(params).__name__}"
                )
                params = {}
            params = params or {}

            children = node.get("children")
            if spec.layer == "container":
                unknown = sorted(set(node) - CONTAINER_ALLOWED_KEYS)
                if unknown:
                    problems.append(
                        f"[{DATA_RULE_VIOLATION}] {path}: container may only carry children and "
                        f"identity keys {sorted(CONTAINER_ALLOWED_KEYS)}, found {unknown}"
                    )
                if node.get("source_fields"):
                    problems.append(
                        f"[{DATA_RULE_VIOLATION}] {path}.source_fields: a container must not "
                        f"reference data; move it into an input module"
                    )
                if "text" in params:
                    problems.append(
                        f"[{DATA_RULE_VIOLATION}] {path}.params.text: a container must not carry "
                        f"literal text; use note_block or a param of a preset display module"
                    )
                if children is not None and not isinstance(children, list):
                    problems.append(
                        f"[{DATA_RULE_VIOLATION}] {path}.children: must be a list of module objects"
                    )
                elif children:
                    walk(children, f"{path}.children", level)
            else:
                if children:
                    problems.append(
                        f"[{DATA_RULE_VIOLATION}] {path}.children: {spec.layer} module {spec.key!r} "
                        f"cannot hold children; only containers can"
                    )

    walk(modules, "modules", 0)
    return problems
