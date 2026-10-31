"""Validate a department interface descriptor against the OAMS constraint layer.

This is the gate that keeps an AI-authored interface honest: a descriptor is data,
never code. Anything that tries to smuggle in logic, queries or credentials is
rejected here, before it can reach the renderer or the company aggregate.

Schema history:
  * v0.1 -- the original contract. Modules were an open list of 11 legacy types
    (item_card, progress_bar, ...) plus a "custom" escape hatch. Frozen: documents
    declaring schema_version "0.1" keep exactly the messages they always had, so
    the acceptance suite from the first release still passes unchanged.
  * v0.2 -- modules come from MODULE_REGISTRY (contract §1), containers may nest
    two levels, modules carry a version lock, the interface declares capabilities
    (default-deny) and metrics, and map_to is limited to the frozen standard field
    vocabulary of §5. Every problem is prefixed with a reject code, e.g.
    "[LAYOUT_TOO_DEEP] modules[root].children[grid]: ..." (contract §3).

Why the split matters: the codes are what tools and humans filter on -- "what kind
of thing was refused" -- while the path tells you where. A validator that reports
only the first problem makes a generated descriptor a guessing game, so this one
always collects everything it can.
"""
from __future__ import annotations

from collections.abc import Collection
from typing import Any

from oams.metrics import (
    METRIC_CATALOG,
    validate_metric_binding,
)
from oams.modules import (
    CONTAINER_DEPTH_LIMIT,
    MODULE_NODE_KEYS,
    MODULE_REGISTRY,
    STANDARD_FIELD_VOCAB,
    get_module,
    module_depth,
    module_params,
    param_problem,
    validate_module_tree,
    written_param_names,
)

SCHEMA_VERSIONS = ("0.1", "0.2")
CURRENT_SCHEMA_VERSION = "0.2"
LEGACY_SCHEMA_VERSION = "0.1"

#: Legacy field vocabulary: what a v0.1 descriptor may map to. Kept verbatim so
#: first-release documents keep validating; v0.2 uses STANDARD_FIELD_VOCAB (§5).
STANDARD_FIELDS = (
    "work_item_id", "title", "org_unit_id", "position_id", "status",
    "blocked_reason", "needs_decision", "progress_value", "note", "evidence",
)

STANDARD_STATUSES = ("planned", "in_progress", "blocked", "done")

#: Legacy module types. Void in v0.2 (see MODULE_REGISTRY); retained only so a
#: v0.1 document still validates with the message it always produced.
DEFAULT_MODULE_TYPES = (
    "item_card", "progress_bar", "blocked_card", "decision_card", "evidence_input",
    "owner_block", "timeline", "metric_card", "list_block", "attachment_block",
    "cycle_block",
)

FIELD_TYPES = ("text", "number", "percent", "date", "enum", "link", "file")
DISPLAY_ONLY = "display_only"

# Keys that would turn a descriptor into executable behaviour. Their presence
# anywhere in the document is a hard rejection, not a warning.
FORBIDDEN_KEYS = ("script", "query", "url_fetch", "credential", "eval", "exec",
                  "sql", "token", "secret", "webhook", "import")

VISIBILITIES = ("department", "public")

#: The frozen capability set (contract §4). An interface may only ask for these.
CAPABILITIES: tuple[str, ...] = (
    "entries:write",
    "entries:read:dept",
    "entries:read:self",
    "aggregate:read:dept",
    "aggregate:read:company",
    "entries:link:work_item",
    "export:dept",
)

#: Capabilities that need a recorded second-level approval (concept §4.5).
REVIEWED_CAPABILITIES: tuple[str, ...] = ("aggregate:read:company",)

#: Reject-reason codes (contract §3 / concept §4.4). Every v0.2 problem message
#: starts with one of them, in brackets, before the path.
REJECT_CODES: tuple[str, ...] = (
    "FIELD_UNMAPPED",
    "STATUS_UNMAPPED",
    "METRIC_UNDEFINED",
    "CAPABILITY_ESCALATION",
    "MODULE_NOT_WHITELISTED",
    "LAYOUT_TOO_DEEP",
    "DATA_RULE_VIOLATION",
)

#: Top-level keys that only a v0.2 descriptor may carry.
V02_ONLY_TOP_LEVEL_KEYS = ("capabilities", "metrics")

#: Module keys that only a v0.2 descriptor may carry.
V02_ONLY_MODULE_KEYS = ("children", "module_version")

#: Required top-level keys, unchanged between versions.
REQUIRED_TOP_LEVEL_KEYS = (
    "interface_id", "name", "org_unit_code", "position_codes",
    "modules", "fields", "status_map", "rules", "meta",
)

#: Display modules that read a platform metric through a declared param.
#: A display module may only ever show a *platform* metric (contract §2).
DISPLAY_METRIC_PARAMS: dict[str, tuple[str, ...]] = {
    "metric_card": ("metric_key",),
    "rank_list": ("metric_key",),
    "metric_grid": ("metric_keys",),
    "flow_strip": ("metrics",),
}


class DescriptorError(Exception):
    """Raised with every reason the descriptor was rejected, never just the first."""


class _Problems(list):
    """Collects problems, formatting each with the code prefix when required."""

    def __init__(self, coded: bool) -> None:
        super().__init__()
        self.coded = coded

    def add(self, code: str, path: str, message: str) -> None:
        if self.coded:
            self.append(f"[{code}] {path}: {message}")
        else:
            self.append(f"{path}: {message}")


def _walk(node: Any, path: str, hits: list[tuple[str, str]]) -> None:
    """Collect (path, key) for every forbidden key, so the caller can decide how
    to format it: v0.1 keeps the bare wording it always had, v0.2 goes through
    _Problems.add and therefore carries its reject code (contract §3)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_KEYS:
                hits.append((f"{path}.{key}", key))
            _walk(value, f"{path}.{key}", hits)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", hits)


def _iter_modules(modules: Any, path: str = "modules"):
    """Yield (node, path) for every module in the tree, depth first."""
    if not isinstance(modules, list):
        return
    for index, node in enumerate(modules):
        if not isinstance(node, dict):
            continue
        key = node.get("key")
        label = key if isinstance(key, str) and key else f"#{index}"
        node_path = f"{path}[{label}]"
        yield node, node_path
        yield from _iter_modules(node.get("children"), f"{node_path}.children")


def _module_params(module: dict) -> dict:
    params = module.get("params")
    return params if isinstance(params, dict) else {}


# --------------------------------------------------------------------------- #
# Field vocabulary
# --------------------------------------------------------------------------- #


def _standard_vocab(version: str, strict_v02: bool) -> tuple[str, ...]:
    """The field vocabulary a document of `version` may map to."""
    if version == LEGACY_SCHEMA_VERSION:
        return STANDARD_FIELDS
    if strict_v02:
        return STANDARD_FIELD_VOCAB
    # Migration window: a v0.2 document may still carry v0.1 mappings while it is
    # being moved over, but everything else is judged by the new rules.
    return tuple(dict.fromkeys(STANDARD_FIELD_VOCAB + STANDARD_FIELDS))


# --------------------------------------------------------------------------- #
# Section checks
# --------------------------------------------------------------------------- #


def _check_schema_version(problems: _Problems, version: Any) -> str:
    if version not in SCHEMA_VERSIONS:
        problems.add("DATA_RULE_VIOLATION", "schema_version",
                     f"unsupported {version!r}, expected one of {SCHEMA_VERSIONS}")
        return CURRENT_SCHEMA_VERSION if version is None else str(version)
    return version


def _check_required_keys(problems: _Problems, descriptor: dict) -> None:
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in descriptor:
            problems.add("DATA_RULE_VIOLATION", key, "required top-level key is missing")


def _check_department(problems: _Problems, descriptor: dict,
                      known_org_unit_codes: set[str] | None,
                      known_position_codes: set[str] | None) -> None:
    if known_org_unit_codes is not None:
        code = descriptor.get("org_unit_code")
        if code is not None and code not in known_org_unit_codes:
            problems.add("DATA_RULE_VIOLATION", "org_unit_code", f"unknown department {code!r}")
    positions = descriptor.get("position_codes") or []
    if not isinstance(positions, list) or not positions:
        problems.add("DATA_RULE_VIOLATION", "position_codes", "must list at least one position")
    elif known_position_codes is not None:
        for position in positions:
            if position not in known_position_codes:
                problems.add("DATA_RULE_VIOLATION", "position_codes", f"unknown position {position!r}")


#: Standard fields the product data centre derives itself (contract §5, group D).
#: They stay in STANDARD_FIELD_VOCAB -- they are legitimate canonical names that the
#: platform writes -- but a department descriptor must never map one of its own
#: fields onto them: the style-level sample/approval status is derived from the
#: colour-level development records, so a department typing it by hand would fork
#: the truth. Vocabulary membership and map_to permission are two different things.
PLATFORM_DERIVED_FIELDS: tuple[str, ...] = ("sample_status", "approval_status")


def _check_fields(problems: _Problems, descriptor: dict, version: str,
                  strict_v02: bool) -> tuple[list[dict], set[str], dict[str, dict], set[str]]:
    """Fields must map somewhere real, and display-only fields cannot aggregate."""
    vocab = _standard_vocab(version, strict_v02)
    fields = descriptor.get("fields")
    if not isinstance(fields, list) or not fields:
        problems.add("FIELD_UNMAPPED", "fields", "must contain at least one field")
        fields = []

    field_keys: set[str] = set()
    by_key: dict[str, dict] = {}
    mapped_fields: set[str] = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            problems.add("FIELD_UNMAPPED", f"fields[{index}]", "must be an object")
            continue
        key = field.get("key")
        if not key:
            problems.add("FIELD_UNMAPPED", f"fields[{index}]", "key is required")
            continue
        if key in field_keys:
            problems.add("FIELD_UNMAPPED", f"fields[{key}]", "duplicate key")
        field_keys.add(key)
        by_key[key] = field
        map_to = field.get("map_to")
        if map_to is None:
            problems.add("FIELD_UNMAPPED", f"fields[{key}]", "map_to is required")
        elif map_to == DISPLAY_ONLY:
            if field.get("aggregatable") is not False:
                problems.add("DATA_RULE_VIOLATION", f"fields[{key}]",
                             "map_to=display_only requires aggregatable=false")
        elif map_to in PLATFORM_DERIVED_FIELDS:
            # In-vocabulary but never map-able, in every schema version: the
            # platform derives this value from the colour-level records.
            problems.add(
                "DATA_RULE_VIOLATION", f"fields[{key}]",
                f"map_to {map_to!r} 是平台派生口径：款级打样与审批状态由商品数据中心"
                "颜色级记录派生，部门不得填报；请改用 'status' 或 'display_only'",
            )
        elif map_to in vocab:
            mapped_fields.add(map_to)
        else:
            problems.add("FIELD_UNMAPPED", f"fields[{key}]",
                         f"map_to {map_to!r} is not a standard field or {DISPLAY_ONLY}")
        if field.get("type") not in FIELD_TYPES:
            problems.add("DATA_RULE_VIOLATION", f"fields[{key}]",
                         f"type {field.get('type')!r} is not one of {FIELD_TYPES}")
        if not field.get("label"):
            problems.add("DATA_RULE_VIOLATION", f"fields[{key}]", "label is required")
        if "aggregatable" not in field:
            problems.add("DATA_RULE_VIOLATION", f"fields[{key}]", "aggregatable is required")
        if not field.get("example"):
            problems.add("DATA_RULE_VIOLATION", f"fields[{key}]", "example is required")
    return fields, field_keys, by_key, mapped_fields


def _check_status_map(problems: _Problems, descriptor: dict) -> None:
    status_map = descriptor.get("status_map")
    if not isinstance(status_map, dict) or not status_map:
        problems.add("STATUS_UNMAPPED", "status_map", "must map at least one department status word")
        return
    for word, target in status_map.items():
        if target not in STANDARD_STATUSES:
            problems.add("STATUS_UNMAPPED", f"status_map[{word}]",
                         f"{target!r} is not one of {STANDARD_STATUSES}")


def _check_modules_legacy(problems: _Problems, modules: list, fields: list[dict],
                          field_keys: set[str], mapped_fields: set[str],
                          produced: set[str]) -> None:
    """v0.1 module rules: legacy types, constrained custom modules, field refs."""
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            problems.add("MODULE_NOT_WHITELISTED", f"modules[{index}]", "must be an object")
            continue
        key = module.get("key") or f"#{index}"
        mtype = module.get("type")

        for source in module.get("source_fields", []) or []:
            if source not in field_keys:
                problems.add("FIELD_UNMAPPED", f"modules[{key}]",
                             f"source_fields references unknown field {source!r}")
            else:
                field = next((f for f in fields if isinstance(f, dict) and f.get("key") == source), None)
                if field and field.get("map_to") in STANDARD_FIELDS:
                    produced.add(field["map_to"])

        if mtype == "custom":
            custom = module.get("custom")
            if not isinstance(custom, dict):
                problems.add("MODULE_NOT_WHITELISTED", f"modules[{key}]",
                             "type=custom requires a custom declaration")
                continue
            for required in ("purpose", "composed_of", "display"):
                if not custom.get(required):
                    problems.add("DATA_RULE_VIOLATION", f"modules[{key}].custom",
                                 f"{required} is required")
            if custom.get("declares_logic") is not False:
                problems.add("DATA_RULE_VIOLATION", f"modules[{key}].custom",
                             "declares_logic must be false")
            for part in custom.get("composed_of", []) or []:
                if part not in DEFAULT_MODULE_TYPES:
                    problems.add("MODULE_NOT_WHITELISTED", f"modules[{key}].custom.composed_of",
                                 f"{part!r} is not a default module")
        elif mtype not in DEFAULT_MODULE_TYPES:
            problems.add("MODULE_NOT_WHITELISTED", f"modules[{key}]",
                         f"type {mtype!r} must be custom or one of {DEFAULT_MODULE_TYPES}")


def _check_modules_v02(problems: _Problems, modules: Any, fields: list[dict],
                       field_keys: set[str], mapped_fields: set[str],
                       produced: set[str], strict_v02: bool) -> None:
    """v0.2 module rules: registry whitelist, nesting, params, field consumption."""
    # Structural gate first (types, versions, container iron law, depth).
    for message in validate_module_tree(modules):
        problems.append(message)

    if not isinstance(modules, list):
        return

    seen_keys: dict[str, str] = {}
    for module, path in _iter_modules(modules):
        key = module.get("key")
        mtype = module.get("type")
        if not isinstance(key, str) or not key.strip():
            problems.add("DATA_RULE_VIOLATION", path, "key is required")
        else:
            if key in seen_keys:
                problems.add("DATA_RULE_VIOLATION", path,
                             f"duplicate module key {key!r}, already used at {seen_keys[key]}")
            seen_keys[key] = path

        spec = get_module(mtype)
        if spec is None:
            # validate_module_tree already reported the whitelist failure; a node
            # we cannot identify carries no further rules worth guessing at.
            continue

        nested = _module_params(module)
        params = module_params(module)
        written = written_param_names(module)
        for name, declaration in spec.params.items():
            if isinstance(declaration, dict) and declaration.get("required") and name not in written:
                problems.add("DATA_RULE_VIOLATION", f"{path}.params",
                             f"required param {name!r} is missing for module {spec.key!r}")
        for name in nested:
            if name not in spec.params:
                problems.add("DATA_RULE_VIOLATION", f"{path}.params",
                             f"param {name!r} is not declared by module {spec.key!r} "
                             f"(allowed: {sorted(spec.params)})")
        # A declaration is a promise: a value outside it must be refused. Flat keys
        # are a legal way to write a declared param (`rows: 3` beside params.columns)
        # and are validated exactly like the nested shape -- silently ignoring a key
        # is what made an author believe `rows: 3` had taken effect.
        #
        # Containers are excluded on purpose, not by oversight: validate_module_tree
        # (called above, in this same pass) already refuses any container key outside
        # CONTAINER_ALLOWED_KEYS -- key/type/order/children/params/module_version --
        # and that set is *stricter* than MODULE_NODE_KEYS: a container may not carry
        # source_fields or aggregatable at all. Re-checking containers here would
        # report every bad key twice and would loosen the stricter rule to this one.
        # tests/test_descriptor_v02.py locks both halves of that sentence.
        if spec.layer != "container":
            unknown = sorted(set(module) - MODULE_NODE_KEYS - set(spec.params))
            if unknown:
                problems.add("DATA_RULE_VIOLATION", path,
                             f"unknown key(s) {unknown} on module {spec.key!r}; a module node "
                             f"carries {sorted(MODULE_NODE_KEYS)} plus its declared params "
                             f"{sorted(spec.params)}")
        for name in sorted(written):
            problem = param_problem(spec, name, params.get(name))
            if problem is not None:
                where = f"{path}.params.{name}" if name in nested else f"{path}.{name}"
                problems.add("DATA_RULE_VIOLATION", where, problem)

        # A display module may only show a platform metric (contract §2).
        for param_name in DISPLAY_METRIC_PARAMS.get(spec.key, ()):
            if param_name not in written:
                continue
            raw = params.get(param_name)
            refs = raw if isinstance(raw, list) else [raw]
            for ref in refs:
                if not isinstance(ref, str) or not ref.strip():
                    problems.add("METRIC_UNDEFINED", f"{path}.params.{param_name}",
                                 "must reference a platform metric key")
                    continue
                metric = METRIC_CATALOG.get(ref)
                if metric is None or metric.origin != "platform":
                    problems.add("METRIC_UNDEFINED", f"{path}.params.{param_name}",
                                 f"{ref!r} is not a platform metric; display modules may only "
                                 f"reference metrics computed by the data centre")

        # source_fields must name fields this interface declares. A container
        # carrying source_fields was already refused by the structural gate; do not
        # repeat it.
        #
        # Deliberately *not* enforced: that each source field's map_to appears in
        # spec.consumes. consumes names the rendering slots a module offers ("a row
        # shows a title, a status, a progress value"), while map_to names what the
        # department's column *is* in company terms. A department's 款号 column is
        # its row title (map_to=style_code), and its 完成度 column is the row's
        # progress (map_to=percent); refusing those would reject the reference
        # plugin in examples/production-dept.yaml. The aggregate check below is the
        # one that actually guards summary correctness.
        sources = [] if spec.layer == "container" else (module.get("source_fields") or [])
        if not isinstance(sources, list):
            problems.add("FIELD_UNMAPPED", f"{path}.source_fields", "must be a list of field keys")
            sources = []
        for source in sources:
            if source not in field_keys:
                problems.add("FIELD_UNMAPPED", f"{path}.source_fields",
                             f"references unknown field {source!r}")
                continue
            field = fields_by_key(fields, source)
            map_to = field.get("map_to") if field else None
            if map_to in STANDARD_FIELD_VOCAB:
                produced.add(map_to)


def fields_by_key(fields: list[dict], key: str) -> dict | None:
    for field in fields:
        if isinstance(field, dict) and field.get("key") == key:
            return field
    return None


def _check_v01_only_legacy(problems: _Problems, descriptor: dict, version: str) -> None:
    """v0.1 documents must not use schema 0.2 features (contract §3)."""
    if version != LEGACY_SCHEMA_VERSION:
        return
    for key in V02_ONLY_TOP_LEVEL_KEYS:
        if key in descriptor:
            problems.add("DATA_RULE_VIOLATION", key,
                         f"is a schema 0.2 feature; set schema_version={CURRENT_SCHEMA_VERSION!r} "
                         f"or remove it")
    for _module, path in _iter_modules(descriptor.get("modules")):
        for key in V02_ONLY_MODULE_KEYS:
            if key in _module:
                problems.add("DATA_RULE_VIOLATION", f"{path}.{key}",
                             "is a schema 0.2 feature; set schema_version="
                             f"{CURRENT_SCHEMA_VERSION!r} or remove it")


def _check_capabilities(problems: _Problems, descriptor: dict, version: str) -> set[str]:
    raw = descriptor.get("capabilities")
    if raw is None:
        return set()
    if not isinstance(raw, list):
        problems.add("CAPABILITY_ESCALATION", "capabilities", "must be a list of capability strings")
        return set()
    declared: set[str] = set()
    for index, capability in enumerate(raw):
        if not isinstance(capability, str) or not capability.strip():
            problems.add("CAPABILITY_ESCALATION", f"capabilities[{index}]",
                         "must be a capability string")
            continue
        if capability not in CAPABILITIES:
            problems.add("CAPABILITY_ESCALATION", f"capabilities[{capability}]",
                         f"is not a declared capability; the frozen set is {list(CAPABILITIES)}")
            continue
        if capability in declared:
            problems.add("DATA_RULE_VIOLATION", f"capabilities[{capability}]",
                         "is declared twice")
        declared.add(capability)

    # aggregate:read:company reads every department's data, so it needs a
    # recorded second-level approval (concept §4.5).  Whether that approval is
    # already on the document is a *process* question, not a shape question --
    # see `recorded_approval_gaps`.  Here we only make sure the declaration is
    # legal; an unreviewed draft has to be allowed to say what it is asking for,
    # or nobody could ever submit the page this approval is about.
    return declared


def _check_metrics(problems: _Problems, descriptor: dict, version: str,
                   fields_map: dict[str, dict]) -> None:
    raw = descriptor.get("metrics")
    if raw is None:
        return
    if not isinstance(raw, list):
        problems.add("METRIC_UNDEFINED", "metrics", "must be a list of metric bindings")
        return
    org_unit_code = descriptor.get("org_unit_code")
    if not isinstance(org_unit_code, str) or not org_unit_code:
        org_unit_code = ""
    seen: set[str] = set()
    for index, binding in enumerate(raw):
        if isinstance(binding, dict):
            metric_key = binding.get("metric_key")
            if isinstance(metric_key, str) and metric_key in seen:
                problems.add("METRIC_UNDEFINED", f"metrics[{metric_key}]",
                             "duplicate metric binding")
            if isinstance(metric_key, str):
                seen.add(metric_key)
        problems.extend(validate_metric_binding(binding, org_unit_code, fields_map))


def _check_visibility(problems: _Problems, descriptor: dict) -> None:
    meta = descriptor.get("meta")
    if not isinstance(meta, dict):
        problems.add("DATA_RULE_VIOLATION", "meta", "required")
        return
    visibility = meta.get("visibility")
    if visibility not in VISIBILITIES:
        problems.add("DATA_RULE_VIOLATION", "meta.visibility", f"must be one of {VISIBILITIES}")


# --------------------------------------------------------------------------- #
# Recorded approval: who signs, and where that signature lives
# --------------------------------------------------------------------------- #

#: The three fields a recorded approval needs to say anything at all. A status
#: without a person is a rumour, and a person without a time cannot be audited.
APPROVAL_FIELDS: tuple[str, ...] = ("status", "reviewed_by", "reviewed_at")


def _approval_gaps(descriptor: Any,
                   known_employee_codes: Collection[str] | None = None) -> list[tuple[str, str, str]]:
    """What a descriptor still lacks when it claims something only a sign-off allows.

    Returned as (code, path, message) so `validate_descriptor` can print them
    with the same coding rules as every other problem, and so a caller that only
    wants the list can print them itself.

    Two claims need a signature (concept §4.5, contract §4): going `public`, and
    asking for `aggregate:read:company`. Everywhere else a department decides
    alone and no signature is owed. This is a *gate*, not a shape check: a page
    waiting for review is supposed to be missing exactly this, which is why
    `validate_descriptor` only looks at it when the caller says so.

    `known_employee_codes` answers the question a document cannot answer about
    itself: is the person named in `reviewed_by` someone who actually works
    here? A signature is a fact about the organisation, not a string the author
    picks, so a hand-written "approved by 王二" must be refused exactly where a
    hand-written "approved" already was. Pass ``None`` when there is no database
    to ask (the offline CLI): the shape is still checked, and the caller says so
    instead of pretending the name was verified.
    """
    if not isinstance(descriptor, dict):
        return []
    meta = descriptor.get("meta") if isinstance(descriptor.get("meta"), dict) else {}
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    complete = all(review.get(field) for field in APPROVAL_FIELDS) and review.get("status") == "approved"
    # A name is not a signer. Whoever is named has to be an active member of this
    # company; without that, "reviewed_by: 王二" is just another sentence the
    # author typed about themselves, which is the whole hole this closes.
    stranger = _stranger_signature(review, known_employee_codes)
    if complete and stranger is None:
        return []
    gaps: list[tuple[str, str, str]] = []
    if meta.get("visibility") == "public":
        # `status` is the one field where any non-empty value is not enough: a
        # page that says "draft" says so on purpose, and counting that as filled
        # in is how a rejected page would read as approved.
        if review.get("status") != "approved":
            gaps.append(("DATA_RULE_VIOLATION", "meta.review",
                         "public templates require status=approved"))
        for field in ("reviewed_by", "reviewed_at"):
            if not review.get(field):
                gaps.append(("DATA_RULE_VIOLATION", "meta.review",
                             f"public templates require {field}"))
    if stranger is not None:
        gaps.append(("DATA_RULE_VIOLATION", "meta.review",
                     "recorded approval must name an active member: "
                     f"reviewed_by {stranger!r} is not an employee code in this company"))
    declared = descriptor.get("capabilities")
    if isinstance(declared, (list, tuple)):
        for capability in sorted({str(item) for item in declared} & set(REVIEWED_CAPABILITIES)):
            gaps.append(("CAPABILITY_ESCALATION", f"capabilities[{capability}]",
                         "needs a recorded second-level approval: meta.review must carry "
                         "status=approved, reviewed_by and reviewed_at"))
    return gaps


def _stranger_signature(review: dict, known_employee_codes: Collection[str] | None) -> str | None:
    """The `reviewed_by` value, when it names nobody who works here.

    ``None`` means "not a stranger", and also "cannot tell": with no employee
    list there is nothing to compare against, and inventing a verdict is worse
    than reporting the limit at the door.
    """
    if known_employee_codes is None:
        return None
    signer = review.get("reviewed_by")
    if not isinstance(signer, str) or not signer:
        return None
    return None if signer in known_employee_codes else signer


def recorded_approval_gaps(descriptor: dict[str, Any], *,
                           known_employee_codes: Collection[str] | None = None) -> list[str]:
    """The approval a descriptor claims but has not got, printed for a person.

    Empty list means every claim on this document is backed by a recorded
    signature. Callers that refuse to publish a file without one (`/setup`,
    `oams plugin`) print this; authoring and submitting do not ask, because
    the signature is produced by the review that follows.

    One rule, two doors: the same function runs behind the online gates (which
    pass `known_employee_codes` and therefore also refuse a name that is not an
    active member) and behind the offline `oams plugin check` (which passes
    ``None`` and prints that the name could not be verified here). Splitting it
    into a "strict" copy would let the two drift, and the drift would be silent.
    """
    coded = not isinstance(descriptor, dict) or descriptor.get("schema_version") != LEGACY_SCHEMA_VERSION
    return [f"[{code}] {path}: {message}" if coded else f"{path}: {message}"
            for code, path, message in _approval_gaps(descriptor, known_employee_codes)]


def _check_aggregate(problems: _Problems, mapped_fields: set[str], produced: set[str]) -> None:
    """The interface must be able to produce every field the company aggregate needs.

    title/status/evidence are the minimum a manager needs to read a line;
    department, position, time and reporter come from the session and the item.
    """
    for required in ("title", "status", "evidence"):
        if required not in mapped_fields and required not in produced:
            problems.add("FIELD_UNMAPPED", "aggregate",
                         f"no field maps to {required!r}, so this interface cannot be summarised")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def validate_descriptor(descriptor: dict[str, Any],
                        known_org_unit_codes: set[str] | None = None,
                        known_position_codes: set[str] | None = None,
                        *,
                        strict_v02: bool = True,
                        require_recorded_approval: bool = False,
                        known_employee_codes: Collection[str] | None = None) -> list[str]:
    """Return a list of problems. Empty list means the descriptor may be rendered.

    `strict_v02` (default on) makes a v0.2 document use only the v0.2 module
    registry and the §5 field vocabulary. Turning it off opens the migration
    window: v0.1 field mappings are tolerated in a v0.2 document. Everything else
    -- capabilities, metrics, nesting, the forbidden-key scan -- always applies.

    `require_recorded_approval` is off by default and off is the honest default:
    a page being written or waiting for review is *supposed* to be missing the
    signature that this flag demands. Only the paths that publish a file without
    a review turn it on (`/setup/interfaces`, `oams plugin`); the page that
    does get reviewed is signed by the approval itself, not by its author.

    `known_employee_codes` is the employee-code list of the company's active
    members. A path that has a database must pass it: a signature only counts
    when the person named works here, and "approved by 王二" typed into JSON has
    to fail where "approved" already failed. Offline callers pass ``None`` and
    say in their report that the name went unverified.
    """
    if not isinstance(descriptor, dict):
        return ["descriptor must be a JSON object"]

    version = descriptor.get("schema_version")
    # 带不带错误码只看文档版本（契约 §3：v0.2 每条问题消息前缀必须带码），
    # 与迁移开关 strict_v02 无关——那个开关只管字段词表。
    coded = version != LEGACY_SCHEMA_VERSION
    problems = _Problems(coded)

    version = _check_schema_version(problems, version)
    _check_required_keys(problems, descriptor)
    _check_department(problems, descriptor, known_org_unit_codes, known_position_codes)

    fields, field_keys, by_key, mapped_fields = _check_fields(
        problems, descriptor, version, strict_v02
    )

    _check_status_map(problems, descriptor)

    _check_v01_only_legacy(problems, descriptor, version)

    modules = descriptor.get("modules")
    produced: set[str] = set()
    if version == LEGACY_SCHEMA_VERSION:
        # v0.1 keeps its original wording; only the qualified types it always had.
        if not isinstance(modules, list) or not modules:
            problems.add("MODULE_NOT_WHITELISTED", "modules", "must contain at least one module")
            modules = []
        _check_modules_legacy(problems, modules, fields, field_keys, mapped_fields, produced)
    else:
        # v0.2 lets the structural gate speak for itself (empty / non-list trees).
        _check_modules_v02(problems, modules, fields, field_keys, mapped_fields,
                           produced, strict_v02)

    # No executable or credential material anywhere in the document.
    # _walk 收集 (path, key)，由这里决定文案：v0.1 保持原裸格式，v0.2 走
    # _Problems.add 带上 reject code（契约 §3）。
    logic_hits: list[tuple[str, str]] = []
    _walk(descriptor, "$", logic_hits)
    for hit_path, _hit_key in logic_hits:
        message = "forbidden key (descriptors must not carry logic or credentials)"
        if version == LEGACY_SCHEMA_VERSION:
            problems.append(f"{hit_path}: {message}")
        else:
            problems.add("DATA_RULE_VIOLATION", hit_path, message)

    _check_visibility(problems, descriptor)
    _check_capabilities(problems, descriptor, version)
    if require_recorded_approval:
        # Only the callers that publish without a review ask for this. Note the
        # order: everything above is about the document's shape; this last block
        # is about the process it has been through.
        for code, path, message in _approval_gaps(descriptor, known_employee_codes):
            problems.add(code, path, message)
    _check_metrics(problems, descriptor, version, by_key)
    _check_aggregate(problems, mapped_fields, produced)

    return list(problems)


def assert_descriptor(descriptor: dict[str, Any], **known: Any) -> None:
    problems = validate_descriptor(descriptor, **known)
    if problems:
        raise DescriptorError("; ".join(problems))


def layout_depth(descriptor: dict) -> int:
    """Convenience: the nesting depth of a descriptor's module tree."""
    return module_depth(descriptor.get("modules") or [])


__all__ = [
    "APPROVAL_FIELDS",
    "CAPABILITIES", "CONTAINER_DEPTH_LIMIT", "CURRENT_SCHEMA_VERSION",
    "DEFAULT_MODULE_TYPES", "DISPLAY_METRIC_PARAMS", "DISPLAY_ONLY",
    "DescriptorError", "FIELD_TYPES", "FORBIDDEN_KEYS", "LEGACY_SCHEMA_VERSION",
    "MODULE_REGISTRY", "PLATFORM_DERIVED_FIELDS", "REJECT_CODES",
    "REQUIRED_TOP_LEVEL_KEYS",
    "REVIEWED_CAPABILITIES", "SCHEMA_VERSIONS", "STANDARD_FIELDS",
    "STANDARD_FIELD_VOCAB", "STANDARD_STATUSES", "V02_ONLY_MODULE_KEYS",
    "V02_ONLY_TOP_LEVEL_KEYS", "VISIBILITIES", "assert_descriptor",
    "layout_depth", "recorded_approval_gaps", "validate_descriptor",
]
