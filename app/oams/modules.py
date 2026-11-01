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
    aggregatable is None (not applicable, not "no"). 唯一的例外是 `lookup_box`
    （查件框）：它不是数据来源，也不是「本部门专属的一份数据」，所以声明
    aggregatable=False —— 只读视角整块剔除它，页面上也不给它挂「本部门可见，
    不参与公司汇总」那句话（2026-11-01 定，形态见 docs/design/lookup-box.md）。

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
    # oams.descriptor.PLATFORM_DERIVED_FIELDS). Read this line as "the platform
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

#: Every key a module node may carry besides its own declared params (contract §3).
#: Anything else is refused instead of ignored: a silently dropped key reads to the
#: author as "it worked".
#:
#: `aggregatable` is here on purpose: a v0.1 node carried it as a flag, and the
#: upgrader keeps `aggregatable: false` on the v0.2 node because it means "this
#: department only, never in the company rollup" (see oams.upgrade). It is not a
#: param -- no module declares it -- so it is tolerated by name, nothing more.
MODULE_NODE_KEYS: frozenset[str] = frozenset(
    {
        "key", "type", "order", "params", "module_version", "children",
        "source_fields", "aggregatable",
    }
)


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


def _param(
    type_name: str,
    *,
    default: Any = None,
    required: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    """One param declaration. `minimum` / `maximum` are emitted only for ranged ints.

    The range is part of the declaration, not just of the renderer's clamp: an
    author who writes `rows: 9` must be told, not silently given five rows.
    """
    declaration: dict[str, Any] = {"type": type_name, "required": required, "default": default}
    if minimum is not None:
        declaration["minimum"] = minimum
    if maximum is not None:
        declaration["maximum"] = maximum
    return declaration


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
        {"columns": _param("list[{key,label}]", required=True),
         "rows": _param("int", default=1, minimum=1, maximum=5)},
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


# 查件框（2026-11-01 加，第 21 个 key）。它按一个标准字段查「正在看的这一版」的条目，
# 所以 aggregatable=False：只读视角会整块剔除它，它也不参与公司汇总——它是查询工具，
# 不是一份数据。`key_field` 必须是本页某个字段映射到的标准字段（descriptor.py 会拦）；
# `label` 只改输入框前面那个词（默认「单号」），不改查询口径。
_register(
    _spec(
        "lookup_box", "查件框", "display",
        "输入单号，就地显示本页条目里的那一条。只查当前这一页，不跨页、不跨部门、不参与汇总。",
        {"key_field": _param("str", default="order_no"), "label": _param("str", default="")},
        aggregatable=False,
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


# --------------------------------------------------------------------------- #
# Param values: nested or flat, never silently dropped
# --------------------------------------------------------------------------- #


def _declared_default(declaration: Any) -> Any:
    if isinstance(declaration, dict) and "default" in declaration:
        return declaration["default"]
    return None


def written_param_names(module: Any) -> set[str]:
    """Param names the author actually wrote, nested (`params.rows`) or flat (`rows`).

    Both shapes are accepted for a *declared* param, because an author who writes
    `rows: 3` next to `params.columns` means it and must not be ignored. A name
    that the module did not declare is not a param in either shape -- the
    descriptor validator refuses it as an unknown node key.
    """
    if not isinstance(module, dict):
        return set()
    spec = get_module(module.get("type"))
    declared = set(spec.params) if spec is not None else set()
    written: set[str] = set()
    nested = module.get("params")
    if isinstance(nested, dict):
        written |= {name for name in nested if name in declared}
    written |= {name for name in declared if name in module}
    return written


def module_params(module: Any) -> dict[str, Any]:
    """Declared param values for one node: registry defaults, `params`, then flat keys.

    Precedence: `params.<name>` wins over a flat `<name>` when both are present,
    since that is the shape the contract documents. The renderer and the validator
    both read params through here, so they can never disagree about what a node
    asked for.
    """
    if not isinstance(module, dict):
        return {}
    spec = get_module(module.get("type"))
    merged: dict[str, Any] = {}
    if spec is not None:
        for name, declaration in spec.params.items():
            merged[name] = _declared_default(declaration)
    nested = module.get("params")
    given = nested if isinstance(nested, dict) else {}
    merged.update(given)
    spec_params = spec.params if spec is not None else {}
    for name in spec_params:
        if name in module and name not in given:
            merged[name] = module[name]
    return merged


def param_problem(spec: ModuleSpec, name: str, value: Any) -> str | None:
    """Why `value` cannot serve as param `name`, or None when it is fine.

    Only the types that carry a range are checked here (`int` with `minimum` /
    `maximum`); everything else is validated where it is rendered.
    """
    declaration = spec.params.get(name)
    if not isinstance(declaration, dict):
        return None
    type_name = declaration.get("type") or ""
    if type_name != "int":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"param {name!r} of module {spec.key!r} must be an integer, got {value!r}"
    minimum = declaration.get("minimum")
    maximum = declaration.get("maximum")
    if minimum is not None and value < minimum:
        return f"param {name!r} of module {spec.key!r} must be at least {minimum}, got {value}"
    if maximum is not None and value > maximum:
        return f"param {name!r} of module {spec.key!r} must be at most {maximum}, got {value}"
    return None


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
