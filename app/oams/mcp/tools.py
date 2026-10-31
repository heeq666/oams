"""Tool layer for the OAMS MCP server.

Design notes (why it looks like this):

* **No SDK in this file.** Every tool is a plain function plus a JSON Schema.
  `server.py` is a thin adapter that wraps these into `mcp.types.Tool`. That
  keeps the whole rule surface -- capability checks, version discipline, audit --
  testable without a transport, a subprocess or a live event loop.
* **Contract modules are imported lazily.** `oams.modules`, `oams.metrics`,
  `oams.render` and `oams.descriptor` are written in parallel by other
  members of the team. If one of them is missing or still on an older signature,
  the affected tool returns a diagnosable error instead of taking the server down
  at import time (an MCP server that fails to boot is useless to the reviewer too).
* **Writes are append-only.** No tool edits a version in place; every mutation
  produces a new `version_no`. A pending or active version can never be
  overwritten -- the AI would otherwise be able to quietly change what a human is
  about to approve.
"""
from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from oams.mcp.audit import (
    ACTOR_AGENT,
    ACTOR_HUMAN,
    AuditEntry,
    AuditStore,
    MemoryAuditStore,
)
from oams.mcp.capabilities import (
    ACTOR_AGENT as _CAP_ACTOR_AGENT,
    CAP_PLUGIN_APPROVE,
    CAP_PLUGIN_READ,
    CAP_PLUGIN_WRITE_DRAFT,
    Capability,
    CapabilityDenied,
    ensure_human,
    issue_agent_capability,
)
from oams.mcp.store import (
    STATUS_DRAFT,
    STATUS_PENDING,
    MemoryDraftStore,
    PluginVersion,
    StoreError,
)

# --------------------------------------------------------------------------- #
# Error codes
# --------------------------------------------------------------------------- #
#: The caller lacks the capability the tool requires.
ERR_CAPABILITY_DENIED = "CAPABILITY_DENIED"
#: The AI tried to approve its own submission (the one thing it must never do).
ERR_HUMAN_CONFIRMATION_REQUIRED = "HUMAN_CONFIRMATION_REQUIRED"
#: plugin_id is unknown.
ERR_PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
#: Bad argument shape (wrong type, missing key, unknown enum value).
ERR_INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
#: The contract module (modules/metrics/render/descriptor) is missing or drifted.
ERR_CONTRACT_UNAVAILABLE = "CONTRACT_UNAVAILABLE"
#: descriptor validation failed; `errors` carries the [CODE]-prefixed messages.
ERR_DESCRIPTOR_INVALID = "DESCRIPTOR_INVALID"
#: store-level refusal (VERSION_FROZEN, PLUGIN_EXISTS, ALREADY_SUBMITTED, ...).
ERR_STORE = "STORE_ERROR"
#: unexpected server-side failure.
ERR_INTERNAL = "INTERNAL_ERROR"

#: Frozen capability vocabulary (contract §4).
CAPABILITY_VOCABULARY: tuple[str, ...] = (
    "entries:write",
    "entries:read:dept",
    "entries:read:self",
    "aggregate:read:dept",
    "aggregate:read:company",
    "entries:link:work_item",
    "export:dept",
)

#: Frozen standard-field vocabulary (contract §5), used by list_canonical_fields
#: as a fallback when `oams.descriptor` is still on the 0.1 field list.
FALLBACK_STANDARD_FIELDS: dict[str, tuple[str, ...]] = {
    "object_ref": ("style_code", "skc_code", "color", "sku_code", "wave", "category",
                   "brand", "designer", "style_position", "supplier_code",
                   "material_code", "order_no"),
    "measure": ("count", "quantity", "amount", "percent", "ratio", "duration",
                "metric_value"),
    "time": ("planned_at", "due_at", "completed_at", "reported_at", "status_since"),
    "state": ("status", "sample_status", "approval_status"),
    "content": ("title", "note", "evidence", "evidence_url", "file"),
    "reference": ("work_item_id", "parent_ref"),
}

DISPLAY_ONLY = "display_only"


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    """What every tool returns: a JSON-able payload plus an ok flag.

    The transport turns `ok=False` into `isError: true` and still hands the whole
    payload to the caller -- an AI that gets a rejection needs to read the reason,
    not a stack trace.
    """

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body = dict(self.payload)
        body.setdefault("ok", self.ok)
        if self.error_code:
            body["error_code"] = self.error_code
        return body


def _ok(**payload: Any) -> ToolResult:
    return ToolResult(ok=True, payload=payload)


def _fail(code: str, message: str, **extra: Any) -> ToolResult:
    return ToolResult(ok=False, payload={"message": message, **extra}, error_code=code)


# --------------------------------------------------------------------------- #
# Contract access (lazy, drift-tolerant)
# --------------------------------------------------------------------------- #
class ContractError(Exception):
    """A contract module is missing or still carries an older signature."""


def _import_contract(name: str, *symbols: str) -> Any:
    try:
        module = __import__(f"oams.{name}", fromlist=["*"])
    except ImportError as error:  # pragma: no cover - depends on sibling branches
        raise ContractError(f"oams.{name} is not importable yet: {error}") from error
    for symbol in symbols:
        if not hasattr(module, symbol):
            raise ContractError(f"oams.{name}.{symbol} is not available yet")
    return module


def _call_with_supported_kwargs(func: Callable[..., Any], *args: Any,
                                **kwargs: Any) -> Any:
    """Call `func` passing only the keyword arguments its signature accepts.

    The contract adds optional keywords over time (e.g. `strict_v02`); calling a
    sibling's older implementation with a newer keyword would raise TypeError and
    hide the real validation result.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return func(*args, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD
           for p in signature.parameters.values()):
        return func(*args, **kwargs)
    accepted = {name for name in signature.parameters if name in kwargs}
    return func(*args, **{name: kwargs[name] for name in accepted})


def validate_descriptor(descriptor: dict[str, Any],
                        known_org_unit_codes: set[str] | None = None,
                        known_position_codes: set[str] | None = None,
                        *, strict_v02: bool = True) -> list[str]:
    """Contract §3 `validate_descriptor`, adapted to whatever A has shipped."""
    module = _import_contract("descriptor", "validate_descriptor")
    problems = _call_with_supported_kwargs(
        module.validate_descriptor, descriptor,
        known_org_unit_codes=known_org_unit_codes,
        known_position_codes=known_position_codes,
        strict_v02=strict_v02,
    )
    return [str(problem) for problem in problems or []]


def validate_module_tree_tool(modules: list[dict]) -> list[str]:
    module = _import_contract("modules", "validate_module_tree")
    return [str(problem) for problem in module.validate_module_tree(modules) or []]


def _module_catalog(layer: str | None = None) -> list[dict[str, Any]]:
    module = _import_contract("modules", "list_modules")
    return list(module.list_modules(layer))


def _metric_catalog(group: str | None = None, home_only: bool = False,
                    origin: str | None = None) -> list[dict[str, Any]]:
    module = _import_contract("metrics", "list_metrics")
    return list(module.list_metrics(group=group, home_only=home_only, origin=origin))


def _get_metric(key: str) -> Any:
    module = _import_contract("metrics", "get_metric")
    return module.get_metric(key)


def _render_preview(descriptor: dict, sample: bool = True) -> str:
    module = _import_contract("render", "render_preview")
    return module.render_preview(descriptor, sample=sample)


def _standard_fields() -> dict[str, Any]:
    """Vocabulary for list_canonical_fields, grouped like contract section 5.

    The authoritative source is oams.modules.STANDARD_FIELD_VOCAB (the 0.2
    freeze); oams.descriptor.STANDARD_FIELDS still carries the shorter 0.1 list,
    so it is never used as the canonical answer. The contract's own table is the
    fallback when neither import resolves -- a caller always gets a usable
    vocabulary, plus `source` saying which one it got.
    """
    flat: tuple[str, ...] = ()
    source = "contract_fallback"
    for module_name, symbol in (("modules", "STANDARD_FIELD_VOCAB"),
                                ("descriptor", "STANDARD_FIELD_VOCAB")):
        try:
            module = _import_contract(module_name, symbol)
        except ContractError:
            continue
        flat = tuple(str(item) for item in getattr(module, symbol))
        if flat:
            source = f"{module_name}.{symbol}"
            break

    grouped: dict[str, list[str]] = {
        group: [name for name in names if not flat or name in flat]
        for group, names in FALLBACK_STANDARD_FIELDS.items()
    }
    if flat:
        covered = {name for names in grouped.values() for name in names}
        # Names the freeze added beyond the section-5 grouping (the legacy entry
        # fields) are surfaced rather than silently dropped.
        remaining = [name for name in flat if name not in covered]
        if remaining:
            grouped["extra"] = remaining
    else:
        flat = tuple(name for names in grouped.values() for name in names)
    return {"groups": grouped, "source": source, "display_only": DISPLAY_ONLY,
            "flat": list(flat)}


# --------------------------------------------------------------------------- #
# Execution context
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    """Everything a tool may touch, injected -- never imported as a global.

    A test builds one with an in-memory store and examines it afterwards; the
    server builds one per process.
    """

    store: Any = field(default_factory=MemoryDraftStore)
    audit: AuditStore = field(default_factory=MemoryAuditStore)
    #: Pinned token -- set by the server (one process = one session) or by a test
    #: that wants to act as a reviewer. Left None, each call is issued an agent
    #: token carrying only plugin:read + plugin:write:draft.
    capability: Capability | None = None
    known_org_unit_codes: set[str] | None = None
    known_position_codes: set[str] | None = None
    #: The token resolved for the call currently executing (set by ToolRegistry).
    active: Capability | None = None

    def capability_for(self, session_id: str | None = None) -> Capability:
        if self.active is not None:
            return self.active
        if self.capability is not None:
            return self.capability
        return issue_agent_capability(session_id or "unknown-session")


# --------------------------------------------------------------------------- #
# Tool specs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    capability: str
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]
    read_only: bool = False
    #: True only for submit_for_review: the human-confirmation gate.
    requires_human: bool = False

    def to_sdk_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _schema(properties: dict[str, Any], required: list[str] | None = None,
            **extra: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    schema.update(extra)
    return schema


_PLUGIN_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{1,63}$",
              "description": "插件标识，小写 slug，例如 prod-sample-board"}


# --------------------------------------------------------------------------- #
# Argument helpers
# --------------------------------------------------------------------------- #
def _require(args: dict[str, Any], *names: str) -> list[str]:
    missing = [name for name in names
               if args.get(name) is None or args.get(name) == ""]
    return missing


def _resolve_version(ctx: ToolContext, plugin_id: str,
                     version_no: int | None) -> PluginVersion | None:
    if version_no is None:
        return ctx.store.latest(plugin_id)
    getter = getattr(ctx.store, "get_version", None)
    if getter is None:
        for row in ctx.store.versions(plugin_id):
            if row.version_no == version_no:
                return row
        return None
    return getter(plugin_id, version_no)


def _validate(ctx: ToolContext, descriptor: dict[str, Any]) -> list[str]:
    """Run the contract validator, plus the module-tree rules it delegates to.

    `validate_descriptor` owns field/metric/capability semantics;
    `validate_module_tree` owns container structure. Both must pass, and the
    caller gets every problem rather than the first.
    """
    problems = validate_descriptor(
        descriptor,
        known_org_unit_codes=ctx.known_org_unit_codes,
        known_position_codes=ctx.known_position_codes,
    )
    modules = descriptor.get("modules")
    if isinstance(modules, list) and modules:
        problems.extend(validate_module_tree_tool(modules))
    return problems


def _write_version(ctx: ToolContext, plugin_id: str,
                   descriptor: dict[str, Any]) -> ToolResult:
    """Shared tail of every descriptor mutation: validate, then append a version."""
    actor = ACTOR_AGENT
    problems = _validate(ctx, descriptor)
    row = ctx.store.append_draft_version(
        plugin_id=plugin_id, descriptor=descriptor, actor=actor,
    )
    return _ok(
        plugin_id=plugin_id,
        version_no=row.version_no,
        status=row.status,
        valid=not problems,
        errors=problems,
        message=("draft saved; fix the reported problems before submitting"
                 if problems else "draft saved"),
    )


def _head_descriptor(ctx: ToolContext, plugin_id: str) -> dict[str, Any] | None:
    head = ctx.store.latest(plugin_id)
    if head is None:
        return None
    return copy.deepcopy(head.descriptor)


def _aggregates_preview(descriptor: dict[str, Any]) -> dict[str, Any]:
    """What the company aggregate would show for this descriptor, from the catalog.

    Read-only and side-effect free: it resolves the declared bindings against
    `oams.metrics` and reports which ones are unresolvable. It never computes
    a real number -- preview is about shape, not data.
    """
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for binding in descriptor.get("metrics") or []:
        if not isinstance(binding, dict):
            unresolved.append(str(binding))
            continue
        key = str(binding.get("metric_key") or "")
        try:
            spec = _get_metric(key)
        except ContractError as error:
            return {"resolved": [], "unresolved": [key],
                    "note": f"metric catalog unavailable: {error}"}
        if spec is None:
            unresolved.append(key)
            continue
        to_dict = getattr(spec, "to_dict", None)
        resolved.append({
            "metric_key": key,
            "mode": binding.get("mode"),
            "agg": binding.get("agg"),
            "from_field": binding.get("from_field"),
            "where_status": binding.get("where_status"),
            "spec": to_dict() if callable(to_dict) else {
                "key": getattr(spec, "key", key),
                "name": getattr(spec, "name", key),
                "unit": getattr(spec, "unit", ""),
            },
        })
    return {"resolved": resolved, "unresolved": unresolved,
            "count": len(resolved)}


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #
def _t_list_modules(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        modules = _module_catalog(args.get("layer"))
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
    except ValueError as error:  # unknown layer
        return _fail(ERR_INVALID_ARGUMENTS, str(error))
    by_layer: dict[str, list[str]] = {}
    for module in modules:
        by_layer.setdefault(str(module.get("layer")), []).append(str(module.get("key")))
    return _ok(modules=modules, count=len(modules), by_layer=by_layer)


def _t_list_canonical_fields(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    vocabulary = _standard_fields()
    flat = sorted({name for names in vocabulary["groups"].values() for name in names})
    return _ok(fields=vocabulary["groups"], flat=flat, count=len(flat),
               display_only=vocabulary["display_only"], source=vocabulary["source"])


def _metric_binding_rules(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """回答「我能绑什么」——目录为空时这段规则就是唯一的答案。

    调用方（外部 AI）问这个工具，真正要的是「下一个 descriptor 里指标该怎么写」。
    平台指标下线后 `METRIC_CATALOG` 是空表，只回车「0 条」等于没有答案；而绑定规则
    本身是稳定的，与目录里有没有东西无关，所以把它作为返回内容的一部分。
    规则原文见契约 §2/§5.2，这里只做机器可读的镜像，别在这里发明新规矩。
    """
    platform_count = sum(1 for m in metrics if m.get("origin") == "platform")
    contract = _import_contract("metrics", "AGG_MODES", "ORIGINS", "GROUPS")
    return {
        "platform": {
            "available": platform_count > 0,
            "count": platform_count,
            "note": (
                "平台指标原由商品数据中心计算，已随商品中心下线一并移除，目录目前为空。"
                "规则保留：将来若有平台指标回到目录，display 绑定可直接引用目录里的键。"
            ),
        },
        "department": {
            "key_pattern": "dept.<ORG_UNIT_CODE>.<slug>",
            "example": "dept.OPS.weekly_done",
            "declare": (
                "在同一个 descriptor 的 metrics[] 里以 mode=emit 声明，metric_key 必须落在"
                " dept.<本单位ORG_UNIT_CODE>. 前缀下；一位部门只能写自己的命名空间。"
            ),
            "aggs": list(contract.AGG_MODES),
            "from_field": "本页 fields[] 里声明过的字段键（emit 必填）",
            "where_status": "可选，限定只统计某个标准状态",
            "display": (
                "展示模块（metric_card/metric_grid/rank_list/flow_strip）的指标参数只能引"
                "本页自己 emit 的键；引 dept.<别的CODE>.* 会被 METRIC_UNDEFINED 拒绝——"
                "跨部门读数是汇总能力的事，不是显示参数能做的事。"
            ),
            "display_binding": "mode=display 的绑定只写 metric_key，不带 agg/from_field/where_status",
        },
        "filters": {
            "origin": list(contract.ORIGINS),
            "group": list(contract.GROUPS),
            "note": "group 分组随平台指标下线，现在没有任何可选值；传 group 会被判为不认识的组。",
        },
        "reject_codes": {
            "METRIC_UNDEFINED": "键非法、未声明，或引了不存在/不属于本页的指标",
            "DATA_RULE_VIOLATION": "字段用法违规（如 emit 用了别部门命名空间、display 带了聚合字段）",
            "FIELD_UNMAPPED": "emit 的 from_field 没有映射到标准字段",
        },
    }


def _t_list_metric_catalog(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    home_only = bool(args.get("home_only") or False)
    try:
        metrics = _metric_catalog(group=args.get("group"), home_only=home_only,
                                  origin=args.get("origin"))
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
    except ValueError as error:
        return _fail(ERR_INVALID_ARGUMENTS, str(error))
    home = [m.get("key") for m in metrics if m.get("on_home")]
    try:
        can_bind = _metric_binding_rules(metrics)
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
    return _ok(metrics=metrics, count=len(metrics), home=home, can_bind=can_bind,
               message=("指标目录本身现在是空的（平台指标已下线）；下一个 descriptor 能绑什么，"
                        "看 can_bind：部门指标在 metrics[] 里用 mode=emit 自己声明，"
                        "展示模块只能引本页自己 emit 的键。"))


def _t_get_plugin(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    plugin_id = str(args.get("plugin_id") or "")
    version_no = args.get("version_no")
    row = _resolve_version(ctx, plugin_id, version_no)
    if row is None:
        return _fail(ERR_PLUGIN_NOT_FOUND,
                     f"plugin {plugin_id!r} version {version_no!r} does not exist")
    return _ok(**row.to_dict(), versions=[v.summary() for v in ctx.store.versions(plugin_id)])


def _t_list_plugins(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rows = ctx.store.list_plugins(org_unit_code=args.get("org_unit_code"),
                                  status=args.get("status"))
    return _ok(plugins=[row.summary() for row in rows], count=len(rows))


def _t_preview_descriptor(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    descriptor = args.get("descriptor")
    if not isinstance(descriptor, dict):
        return _fail(ERR_INVALID_ARGUMENTS, "descriptor must be a JSON object")
    sample = args.get("sample")
    try:
        problems = _validate(ctx, descriptor)
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
    try:
        html = _render_preview(descriptor, sample=True if sample is None else bool(sample))
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error), valid=not problems,
                     errors=problems, rendered_html="", aggregates_preview={})
    except Exception as error:  # RenderError and friends: a preview must never 500
        # A render failure is reported separately: `errors` carries contract
        # codes only, so a client can group problems by code without filtering
        # out a rendering message that is not a contract verdict.
        return _ok(valid=False, errors=problems, rendered_html="",
                   render_error=f"{type(error).__name__}: {error}",
                   aggregates_preview=_aggregates_preview(descriptor),
                   persisted=False,
                   message="descriptor could not be rendered; see render_error")
    return _ok(valid=not problems, errors=problems, rendered_html=html,
               render_error=None,
               aggregates_preview=_aggregates_preview(descriptor),
               # preview reads nothing from the store: no version is created.
               persisted=False)


# --------------------------------------------------------------------------- #
# Draft-writing tools
# --------------------------------------------------------------------------- #
def _t_create_draft_plugin(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    missing = _require(args, "plugin_id", "org_unit_code", "name", "descriptor")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    descriptor = args["descriptor"]
    if not isinstance(descriptor, dict):
        return _fail(ERR_INVALID_ARGUMENTS, "descriptor must be a JSON object")
    try:
        problems = _validate(ctx, descriptor)
    except ContractError as error:
        return _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
    try:
        row = ctx.store.create_draft(
            plugin_id=str(args["plugin_id"]),
            org_unit_code=str(args["org_unit_code"]),
            name=str(args["name"]),
            descriptor=copy.deepcopy(descriptor),
            actor=ACTOR_AGENT,
        )
    except StoreError as error:
        return _fail(error.code, str(error))
    return _ok(plugin_id=row.plugin_id, version_no=row.version_no, status=row.status,
               valid=not problems, errors=problems,
               message="draft v%d created in status=draft" % row.version_no)


def _t_patch_draft_module(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    missing = _require(args, "plugin_id", "module")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    plugin_id = str(args["plugin_id"])
    module = args["module"]
    if not isinstance(module, dict):
        return _fail(ERR_INVALID_ARGUMENTS, "module must be a JSON object")
    descriptor = _head_descriptor(ctx, plugin_id)
    if descriptor is None:
        return _fail(ERR_PLUGIN_NOT_FOUND, f"plugin {plugin_id!r} does not exist")
    modules = descriptor.get("modules")
    if not isinstance(modules, list):
        modules = []
        descriptor["modules"] = modules

    index = args.get("index")
    key = args.get("module_key") or module.get("key")
    target: int | None = None
    if isinstance(index, int) and 0 <= index < len(modules):
        target = index
    elif key:
        for position, existing in enumerate(modules):
            if isinstance(existing, dict) and existing.get("key") == key:
                target = position
                break
    if target is None:
        if args.get("append", True) is False:
            return _fail(ERR_INVALID_ARGUMENTS,
                         f"module {key!r} not found and append=false")
        modules.append(copy.deepcopy(module))
        action = "appended"
    else:
        modules[target] = copy.deepcopy(module)
        action = "replaced"
    result = _write_version(ctx, plugin_id, descriptor)
    result.payload["action"] = action
    return result


def _t_set_field_mapping(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    missing = _require(args, "plugin_id", "field_key", "map_to")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    plugin_id = str(args["plugin_id"])
    descriptor = _head_descriptor(ctx, plugin_id)
    if descriptor is None:
        return _fail(ERR_PLUGIN_NOT_FOUND, f"plugin {plugin_id!r} does not exist")
    fields = descriptor.get("fields")
    if not isinstance(fields, list):
        return _fail(ERR_INVALID_ARGUMENTS, "descriptor has no fields list")
    field_key = str(args["field_key"])
    target = next((f for f in fields
                   if isinstance(f, dict) and f.get("key") == field_key), None)
    if target is None:
        # Declaring a new field is legitimate; the contract validator decides
        # whether the mapping is acceptable, so we do not pre-judge it here.
        target = {"key": field_key}
        fields.append(target)
        action = "created"
    else:
        action = "updated"
    target["map_to"] = args["map_to"]
    if "label" in args:
        target["label"] = args["label"]
    if "type" in args:
        target["type"] = args["type"]
    if "example" in args:
        target["example"] = args["example"]
    if "aggregatable" in args:
        target["aggregatable"] = bool(args["aggregatable"])
    elif args["map_to"] == DISPLAY_ONLY:
        # display_only is not aggregatable by definition; inferring it removes a
        # failure mode that has no other meaning.
        target.setdefault("aggregatable", False)
    result = _write_version(ctx, plugin_id, descriptor)
    result.payload["action"] = action
    result.payload["field_key"] = field_key
    return result


def _t_set_status_map(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    missing = _require(args, "plugin_id", "status_map")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    status_map = args["status_map"]
    if not isinstance(status_map, dict) or not status_map:
        return _fail(ERR_INVALID_ARGUMENTS,
                     "status_map must be a non-empty object of {department_word: standard_status}")
    plugin_id = str(args["plugin_id"])
    descriptor = _head_descriptor(ctx, plugin_id)
    if descriptor is None:
        return _fail(ERR_PLUGIN_NOT_FOUND, f"plugin {plugin_id!r} does not exist")
    merged = dict(descriptor.get("status_map") or {})
    merged.update({str(word): str(target) for word, target in status_map.items()})
    descriptor["status_map"] = merged
    result = _write_version(ctx, plugin_id, descriptor)
    result.payload["status_map"] = merged
    return result


def _t_get_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    missing = _require(args, "plugin_id")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    plugin_id = str(args["plugin_id"])
    head = ctx.store.latest(plugin_id)
    if head is None:
        return _fail(ERR_PLUGIN_NOT_FOUND, f"plugin {plugin_id!r} does not exist")
    try:
        problems = _validate(ctx, head.descriptor)
    except ContractError as error:
        problems = [f"[CONTRACT_UNAVAILABLE] {error}"]
    return _ok(
        plugin_id=plugin_id,
        version_no=head.version_no,
        status=head.status,
        writable=head.is_writable,
        valid=not problems,
        errors=problems,
        descriptor=head.descriptor,
        versions=[v.summary() for v in ctx.store.versions(plugin_id)],
    )


def _t_submit_for_review(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """draft -> pending, and only a human may do it.

    Two independent gates, because one is a checkbox and the other is a right:
     1. `human_confirmed` must be true -- the calling client had a human click
        "submit" (the AI cannot set it truthfully on its own);
     2. the capability must be a *human* one holding `plugin:approve`. An agent
        capability never carries `plugin:approve`, so an AI that calls this tool
        -- even with human_confirmed=true -- is refused here.
    """
    missing = _require(args, "plugin_id")
    if missing:
        return _fail(ERR_INVALID_ARGUMENTS, f"missing required argument(s): {', '.join(missing)}")
    plugin_id = str(args["plugin_id"])
    capability = ctx.capability_for(args.get("session_id"))

    if args.get("human_confirmed") is not True:
        return _fail(
            ERR_HUMAN_CONFIRMATION_REQUIRED,
            "submit_for_review requires human_confirmed=true from an approving human; "
            "the MCP client must show a confirmation step before calling this tool",
            plugin_id=plugin_id,
        )
    try:
        ensure_human(capability, what=f"submitting plugin {plugin_id!r} for review")
        capability.require(CAP_PLUGIN_APPROVE)
    except CapabilityDenied as error:
        return _fail(
            ERR_CAPABILITY_DENIED,
            "an AI session may not submit a plugin for review: "
            "plugin:approve is never granted to an agent token (contract §4 / concept 7.2). "
            "A human must review the draft and submit it from the console.",
            plugin_id=plugin_id,
            required_capability=CAP_PLUGIN_APPROVE,
            subject=error.subject,
        )
    try:
        row = ctx.store.submit(plugin_id=plugin_id, actor=capability.subject,
                               review_note=args.get("review_note") or None)
    except StoreError as error:
        return _fail(error.code, str(error), plugin_id=plugin_id)
    return _ok(plugin_id=plugin_id, version_no=row.version_no, status=row.status,
               submitted_at=row.submitted_at, submitted_by=row.submitted_by,
               message="draft submitted; a reviewer must approve it before it goes live")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_modules",
        description=(
            "列出模块注册表（container/input/display 三层，20 个白名单模块）及其参数、"
            "可消费/产出的标准字段、是否可汇总。descriptor 里的 modules[].type 只能取这里的 key。"
        ),
        input_schema=_schema({
            "layer": {"type": "string", "enum": ["container", "input", "display"],
                      "description": "只返回某一层的模块；省略则返回全部"},
        }),
        capability=CAP_PLUGIN_READ,
        handler=_t_list_modules,
        read_only=True,
    ),
    ToolSpec(
        name="list_canonical_fields",
        description=(
            "列出标准字段词汇表（对象引用/度量/时间/状态/内容/引用六组，外加 display_only）。"
            "descriptor 里 fields[].map_to 只能取这些值或 display_only。"
        ),
        input_schema=_schema({}),
        capability=CAP_PLUGIN_READ,
        handler=_t_list_canonical_fields,
        read_only=True,
    ),
    ToolSpec(
        name="list_metric_catalog",
        description=(
            "回答「一个 descriptor 能绑什么指标」：返回 can_bind 绑定规则（部门指标的键形如"
            " dept.<本单位CODE>.<slug>、须在本页 metrics[] 里以 mode=emit 声明、可用的聚合只有"
            " count/sum/avg/count_distinct、展示模块只能引本页自己 emit 的键），以及目录本身"
            "当前有没有平台指标（目前为空，平台指标已随商品中心下线）。metrics 是目录现状，"
            "can_bind 是规则，规则不会因目录为空而失效。"
        ),
        input_schema=_schema({
            "group": {"type": "string", "enum": ["design", "sample", "order", "money"]},
            "origin": {"type": "string", "enum": ["platform", "interface"]},
            "home_only": {"type": "boolean", "default": False,
                          "description": "只返回首页默认展示的指标"},
        }),
        capability=CAP_PLUGIN_READ,
        handler=_t_list_metric_catalog,
        read_only=True,
    ),
    ToolSpec(
        name="get_plugin",
        description="按 plugin_id 读取插件（默认最新版本，可指定 version_no），返回描述符与全部版本摘要。",
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "version_no": {"type": "integer", "minimum": 1},
        }, required=["plugin_id"]),
        capability=CAP_PLUGIN_READ,
        handler=_t_get_plugin,
        read_only=True,
    ),
    ToolSpec(
        name="list_plugins",
        description="列出插件（按 org_unit_code / status 过滤），返回各插件最新版本的摘要。",
        input_schema=_schema({
            "org_unit_code": {"type": "string", "description": "部门编码，例如 PRODUCTION"},
            "status": {"type": "string", "enum": ["draft", "pending", "active", "archived"]},
        }),
        capability=CAP_PLUGIN_READ,
        handler=_t_list_plugins,
        read_only=True,
    ),
    ToolSpec(
        name="preview_descriptor",
        description=(
            "预览一个描述符：返回 valid / errors / rendered_html / aggregates_preview。"
            "纯只读——不写入任何版本，用假数据渲染。是否进公司汇总只看 aggregates_preview："
            "resolved 是能进汇总的指标绑定（metric_key / mode / agg / from_field / where_status），"
            "unresolved 是解析不上的 binding；页面上不再逐块标注「进汇总/不进汇总」角标。"
            "改完草稿后应先预览再提交。"
        ),
        input_schema=_schema({
            "descriptor": {"type": "object", "description": "完整的 descriptor v0.2 文档"},
            "sample": {"type": "boolean", "default": True,
                       "description": "是否填充示例数据（false 则只渲染空壳）"},
        }, required=["descriptor"]),
        capability=CAP_PLUGIN_READ,
        handler=_t_preview_descriptor,
        read_only=True,
    ),
    ToolSpec(
        name="create_draft_plugin",
        description=(
            "新建一个插件草稿（version_no=1，status=draft）。写入前会跑描述符校验，"
            "errors 非空时草稿仍然保存但需要修正后才能提交。"
        ),
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "org_unit_code": {"type": "string", "description": "所属部门编码"},
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "descriptor": {"type": "object"},
        }, required=["plugin_id", "org_unit_code", "name", "descriptor"]),
        capability=CAP_PLUGIN_WRITE_DRAFT,
        handler=_t_create_draft_plugin,
    ),
    ToolSpec(
        name="patch_draft_module",
        description=(
            "在最新草稿里插入/替换一个模块（按 module_key 或 index 定位）。"
            "每次写入都会产生新的 version_no，永不覆盖 pending/active 版本。"
        ),
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "module_key": {"type": "string", "description": "要替换的模块 key；省略时用 module.key"},
            "index": {"type": "integer", "minimum": 0,
                      "description": "按位置定位（优先于 module_key）"},
            "module": {"type": "object", "description": "完整的模块节点，含 key/type/params/children"},
            "append": {"type": "boolean", "default": True,
                       "description": "找不到目标模块时是否追加；false 则报错"},
        }, required=["plugin_id", "module"]),
        capability=CAP_PLUGIN_WRITE_DRAFT,
        handler=_t_patch_draft_module,
    ),
    ToolSpec(
        name="set_field_mapping",
        description=(
            "设置某个字段的 map_to 映射（标准字段或 display_only）。字段不存在时新建。"
            "写入后返回新的 version_no 与完整校验结果。"
        ),
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "field_key": {"type": "string", "minLength": 1},
            "map_to": {"type": "string",
                       "description": "标准字段名（见 list_canonical_fields）或 display_only"},
            "type": {"type": "string",
                     "enum": ["text", "number", "percent", "date", "enum", "link", "file"]},
            "label": {"type": "string"},
            "example": {"type": "string"},
            "aggregatable": {"type": "boolean"},
        }, required=["plugin_id", "field_key", "map_to"]),
        capability=CAP_PLUGIN_WRITE_DRAFT,
        handler=_t_set_field_mapping,
    ),
    ToolSpec(
        name="set_status_map",
        description=(
            "合并设置部门状态词到标准状态的映射（planned/in_progress/blocked/done）。"
            "未映射的状态词会被校验器以 [STATUS_UNMAPPED] 拦截。"
        ),
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "status_map": {"type": "object", "minProperties": 1,
                           "additionalProperties": {"type": "string",
                                                    "enum": ["planned", "in_progress", "blocked", "done"]},
                           "description": "部门状态词 -> 标准状态"},
        }, required=["plugin_id", "status_map"]),
        capability=CAP_PLUGIN_WRITE_DRAFT,
        handler=_t_set_status_map,
    ),
    ToolSpec(
        name="get_draft",
        description=(
            "读取插件最新版本的草稿内容、完整校验结果、是否可写以及全部版本摘要。"
            "接手一个旧草稿时先调它。"
        ),
        input_schema=_schema({"plugin_id": _PLUGIN_ID}, required=["plugin_id"]),
        capability=CAP_PLUGIN_READ,
        handler=_t_get_draft,
        read_only=True,
    ),
    ToolSpec(
        name="submit_for_review",
        description=(
            "把草稿提交评审（draft -> pending）。只有人类可以调用：必须由人工确认后传入 "
            "human_confirmed=true，且需要 plugin:approve 能力；AI 的 token 永远没有该能力，"
            "所以 AI 无法自我批准。"
        ),
        input_schema=_schema({
            "plugin_id": _PLUGIN_ID,
            "human_confirmed": {"type": "boolean",
                                "description": "必须为 true，表示已有真人在客户端点了确认"},
            "review_note": {"type": "string", "maxLength": 500},
        }, required=["plugin_id", "human_confirmed"]),
        capability=CAP_PLUGIN_APPROVE,
        handler=_t_submit_for_review,
        requires_human=True,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


# --------------------------------------------------------------------------- #
# Dispatch: capability check -> audit -> handler
# --------------------------------------------------------------------------- #
class ToolRegistry:
    """Invokes a tool by name with the three cross-cutting rules applied.

    The order matters and is deliberate:
    1. the capability is checked *before* the body runs (a refusal must not have
       had any side effect);
    2. the audit entry is written for both outcomes -- a denied attempt is the
       single most interesting line in an audit log;
    3. handler failures are converted to a result, never an exception, so one bad
       tool call cannot terminate the stdio session.
    """

    def __init__(self, context: ToolContext | None = None) -> None:
        self.context = context or ToolContext()

    # -- discovery -----------------------------------------------------------
    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return TOOL_SPECS

    def schema_list(self) -> list[dict[str, Any]]:
        return [spec.to_sdk_dict() for spec in TOOL_SPECS]

    def get(self, name: str) -> ToolSpec | None:
        return TOOLS_BY_NAME.get(name)

    # -- invocation ----------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any] | None = None,
             session_id: str | None = None) -> ToolResult:
        arguments = dict(arguments or {})
        spec = self.get(name)
        if spec is None:
            return _fail(ERR_INVALID_ARGUMENTS, f"unknown tool {name!r}")

        capability = (self.context.capability
                      if self.context.capability is not None
                      else issue_agent_capability(session_id or "unknown-session"))
        self.context.active = capability
        plugin_id = arguments.get("plugin_id")
        version_no = arguments.get("version_no")

        try:
            capability.require(spec.capability)
        except CapabilityDenied as error:
            self._audit(name, capability, plugin_id=plugin_id, version_no=version_no,
                        ok=False, error_code=ERR_CAPABILITY_DENIED, detail=str(error))
            return _fail(ERR_CAPABILITY_DENIED, str(error),
                         tool=name, required_capability=spec.capability)

        try:
            result = spec.handler(self.context, arguments)
        except StoreError as error:
            result = _fail(error.code, str(error))
        except ContractError as error:
            result = _fail(ERR_CONTRACT_UNAVAILABLE, str(error))
        except Exception as error:  # noqa: BLE001 - a tool must never crash the server
            result = _fail(ERR_INTERNAL, f"{type(error).__name__}: {error}")

        row_plugin = result.payload.get("plugin_id") or plugin_id
        row_version = result.payload.get("version_no") or version_no
        self._audit(name, capability, plugin_id=row_plugin, version_no=row_version,
                    ok=result.ok, error_code=result.error_code,
                    detail=result.payload.get("action")
                    or result.payload.get("status")
                    or result.payload.get("message"))
        return result

    def _audit(self, tool: str, capability: Capability, *, plugin_id: Any,
               version_no: Any, ok: bool, error_code: str | None,
               detail: str | None) -> None:
        self.context.audit.append(
            tool=tool,
            actor=ACTOR_HUMAN if capability.is_human else ACTOR_AGENT,
            session_id=capability.session_id,
            subject=capability.subject,
            plugin_id=str(plugin_id) if plugin_id else None,
            version_no=int(version_no) if isinstance(version_no, int) else None,
            ok=ok,
            error_code=error_code,
            detail=detail,
        )


def build_registry(store: Any = None, audit: AuditStore | None = None,
                   capability: Capability | None = None,
                   known_org_unit_codes: set[str] | None = None,
                   known_position_codes: set[str] | None = None) -> ToolRegistry:
    """Construct a registry; the server calls this once per process."""
    return ToolRegistry(ToolContext(
        store=store if store is not None else MemoryDraftStore(),
        audit=audit if audit is not None else MemoryAuditStore(),
        capability=capability,
        known_org_unit_codes=known_org_unit_codes,
        known_position_codes=known_position_codes,
    ))
