"""Tests for the Workhub MCP server tool layer.

Scope: everything that decides *what an AI may do* through MCP.

    JSON Schema of the 12 tools  ->  tools.py dispatch  ->  store/capability/audit

No transport, no subprocess, no PostgreSQL: the store is an in-memory fake
(store.MemoryDraftStore), so these tests run anywhere and pin the rules rather
than an environment. The `mcp` SDK is only touched where the adapter itself is
under test, and that case skips if the package is absent.

The three rules these tests exist to protect:
  1. an AI token carries plugin:read + plugin:write:draft and *never*
     plugin:approve, so an AI can never submit its own work for review;
  2. every mutation appends a new version_no -- a pending version is never
     rewritten under the reviewer's feet;
  3. a preview has no side effects at all, so an AI can iterate freely.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub import descriptor as descriptor_contract
from workhub import metrics as metrics_contract
from workhub import modules as modules_contract
from workhub.mcp import server
from workhub.mcp.audit import ACTOR_AGENT, ACTOR_HUMAN
from workhub.mcp.capabilities import (
    CAP_PLUGIN_APPROVE,
    CAP_PLUGIN_READ,
    CAP_PLUGIN_WRITE_DRAFT,
    CapabilityDenied,
    issue_agent_capability,
    issue_human_capability,
)
from workhub.mcp.store import MemoryDraftStore
from workhub.mcp.tools import (
    ERR_CAPABILITY_DENIED,
    ERR_HUMAN_CONFIRMATION_REQUIRED,
    ERR_INVALID_ARGUMENTS,
    TOOL_SPECS,
    TOOLS_BY_NAME,
    build_registry,
)

TOOL_NAMES = {
    "list_modules",
    "list_canonical_fields",
    "list_metric_catalog",
    "get_plugin",
    "list_plugins",
    "preview_descriptor",
    "create_draft_plugin",
    "patch_draft_module",
    "set_field_mapping",
    "set_status_map",
    "get_draft",
    "submit_for_review",
}

READ_ONLY_TOOLS = {
    "list_modules",
    "list_canonical_fields",
    "list_metric_catalog",
    "get_plugin",
    "list_plugins",
    "preview_descriptor",
    "get_draft",
}


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def good_descriptor(**overrides):
    """A descriptor that satisfies every rule of contract v0.2."""
    descriptor = {
        "schema_version": "0.2",
        "interface_id": "prod-sample-board",
        "name": "生产打样看板",
        "org_unit_code": "PRODUCTION",
        "position_codes": ["PROD_MGR"],
        "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
        "metrics": [{"metric_key": "funnel.sample_done", "mode": "display"}],
        "modules": [
            {"key": "row", "type": "progress_row", "source_fields": ["title", "status"]},
            {"key": "note", "type": "note_block", "params": {"text": "备注"}},
        ],
        "fields": [
            {"key": "title", "map_to": "title", "type": "text", "label": "标题",
             "aggregatable": False, "example": "AW26-001 羊毛混纺外套"},
            {"key": "status", "map_to": "status", "type": "enum", "label": "状态",
             "aggregatable": True, "example": "生产"},
            {"key": "evidence", "map_to": "evidence", "type": "text", "label": "证据",
             "aggregatable": False, "example": "https://example.invalid/a.png"},
        ],
        "status_map": {"生产": "in_progress"},
        "rules": {},
        "meta": {"visibility": "department"},
    }
    descriptor.update(overrides)
    return descriptor


class FakeStore:
    """A store the registry has never heard of: same Protocol, different backing.

    It exists to prove the write path talks to the DraftStore interface only --
    swapping the in-memory fake for a PostgreSQL adapter is a one-line change in
    build_registry, not a change to any tool. It also counts writes so a test can
    assert that a refused call wrote nothing.
    """

    def __init__(self):
        self._inner = MemoryDraftStore()
        self.calls = []

    # -- DraftStore ---------------------------------------------------------
    def create_draft(self, **kwargs):
        self.calls.append(("create_draft", kwargs["plugin_id"]))
        return self._inner.create_draft(**kwargs)

    def append_draft_version(self, **kwargs):
        self.calls.append(("append_draft_version", kwargs["plugin_id"]))
        return self._inner.append_draft_version(**kwargs)

    def submit(self, **kwargs):
        self.calls.append(("submit", kwargs["plugin_id"]))
        return self._inner.submit(**kwargs)

    def latest(self, plugin_id):
        return self._inner.latest(plugin_id)

    def get_version(self, plugin_id, version_no):
        return self._inner.get_version(plugin_id, version_no)

    def versions(self, plugin_id):
        return self._inner.versions(plugin_id)

    def list_plugins(self, *, org_unit_code=None, status=None):
        return self._inner.list_plugins(org_unit_code=org_unit_code, status=status)


@pytest.fixture
def registry():
    """An agent session: exactly the token the MCP client hands to the AI."""
    return build_registry(capability=issue_agent_capability("sess-test"))


@pytest.fixture
def human_registry():
    """A reviewer session: a human token holding plugin:approve."""
    return build_registry(capability=issue_human_capability("alice",
                                                          session_id="sess-human"))


def seed(registry, descriptor=None, plugin_id="prod-sample-board"):
    result = registry.call(
        "create_draft_plugin",
        {
            "plugin_id": plugin_id,
            "org_unit_code": "PRODUCTION",
            "name": "生产打样看板",
            "descriptor": descriptor or good_descriptor(),
        },
        session_id="sess-test",
    )
    assert result.ok is True, result.to_dict()
    return plugin_id


# --------------------------------------------------------------------------- #
# 1. Tool definitions: a client must be able to trust tools/list
# --------------------------------------------------------------------------- #
def test_registry_exposes_exactly_the_twelve_contract_tools():
    names = [spec.name for spec in TOOL_SPECS]
    assert len(names) == 12
    assert set(names) == TOOL_NAMES
    assert len(names) == len(set(names)), "tool names must be unique"
    assert set(TOOLS_BY_NAME) == TOOL_NAMES


def test_every_tool_definition_is_a_valid_json_schema_object():
    from jsonschema import Draft202012Validator

    assert server.tool_schemas(), "tools/list must not be empty"
    for schema in server.tool_schemas():
        assert set(schema) == {"name", "description", "inputSchema"}
        assert schema["name"].replace("_", "").isalnum(), schema["name"]
        assert schema["name"] == schema["name"].lower()
        # A description is what the model reads before calling: it must say what
        # the tool is for, not just repeat the name.
        assert len(schema["description"]) >= 30, schema["name"]
        body = schema["inputSchema"]
        Draft202012Validator.check_schema(body)
        assert body["type"] == "object"
        assert body.get("additionalProperties") is False, (
            "typo'd arguments must be rejected, not silently ignored: " + schema["name"]
        )
        required = set(body.get("required") or [])
        properties = set(body.get("properties") or {})
        assert required <= properties, schema["name"]
        for name, prop in (body.get("properties") or {}).items():
            assert "type" in prop or "enum" in prop, f"{schema['name']}.{name}"


def test_required_arguments_match_what_the_handlers_need():
    required = {spec.name: set(spec.input_schema.get("required") or [])
                for spec in TOOL_SPECS}
    assert required["create_draft_plugin"] == {"plugin_id", "org_unit_code", "name",
                                               "descriptor"}
    assert required["patch_draft_module"] == {"plugin_id", "module"}
    assert required["set_field_mapping"] == {"plugin_id", "field_key", "map_to"}
    assert required["set_status_map"] == {"plugin_id", "status_map"}
    assert required["submit_for_review"] == {"plugin_id", "human_confirmed"}
    # Reads with no arguments must not demand any.
    assert required["list_modules"] == set()
    assert required["list_canonical_fields"] == set()
    assert required["list_plugins"] == set()


def test_read_only_tools_are_marked_as_such():
    for spec in TOOL_SPECS:
        assert spec.read_only is (spec.name in READ_ONLY_TOOLS), spec.name
        if spec.read_only:
            assert spec.capability == CAP_PLUGIN_READ


def test_only_submit_for_review_requires_a_human_and_the_approve_capability():
    for spec in TOOL_SPECS:
        if spec.name == "submit_for_review":
            assert spec.requires_human is True
            assert spec.capability == CAP_PLUGIN_APPROVE
        else:
            assert spec.requires_human is False
            assert spec.capability in (CAP_PLUGIN_READ, CAP_PLUGIN_WRITE_DRAFT)


def test_sdk_adapter_accepts_every_schema():
    """The adapter's only job: hand our schemas to the SDK unchanged."""
    types = pytest.importorskip("mcp.types", reason="mcp SDK not installed")
    tools = [types.Tool(name=s["name"], description=s["description"],
                        input_schema=s["inputSchema"])
             for s in server.tool_schemas()]
    assert len(tools) == 12
    dumped = [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]
    assert all("inputSchema" in d for d in dumped)
    assert [d["name"] for d in dumped] == [spec.name for spec in TOOL_SPECS]


def test_the_sdk_server_serves_the_same_tools_and_refusals():
    """Build the real SDK server and drive its handlers, without a transport.

    This is the seam that would otherwise only be exercised by a live stdio
    session: if the SDK ever changes how it hands handlers their params, this
    test fails here instead of in the reviewer's terminal.
    """
    types = pytest.importorskip("mcp.types", reason="mcp SDK not installed")
    import asyncio

    registry = build_registry(capability=issue_agent_capability("stdio-session"))
    sdk_server = server.build_server(registry)
    handlers = sdk_server._request_handlers  # lowlevel Server handler table

    async def drive():
        listed = await handlers["tools/list"].handler(
            None, handlers["tools/list"].params_type())
        called = await handlers["tools/call"].handler(
            None, types.CallToolRequestParams(name="list_modules", arguments={}))
        refused = await handlers["tools/call"].handler(
            None, types.CallToolRequestParams(
                name="submit_for_review",
                arguments={"plugin_id": "x", "human_confirmed": True}))
        return listed, called, refused

    listed, called, refused = asyncio.run(drive())
    assert [tool.name for tool in listed.tools] == [s.name for s in TOOL_SPECS]
    assert all(tool.input_schema for tool in listed.tools)
    assert called.is_error is False
    assert json.loads(called.content[0].text)["count"] == 20
    # A rejection must arrive as a tool error with its code intact, not as a
    # transport-level exception.
    assert refused.is_error is True
    assert refused.structured_content["error_code"] == ERR_CAPABILITY_DENIED


# --------------------------------------------------------------------------- #
# 2. Catalogs are consumed from the contract, never hardcoded
# --------------------------------------------------------------------------- #
def test_list_modules_reads_the_module_registry(registry):
    result = registry.call("list_modules", {}, session_id="sess-test")
    assert result.ok is True
    modules = result.payload["modules"]
    expected = modules_contract.list_modules()
    assert len(modules) == len(expected) == 20
    assert [m["key"] for m in modules] == [m["key"] for m in expected]
    assert result.payload["count"] == 20
    assert set(result.payload["by_layer"]) == {"container", "input", "display"}
    assert len(result.payload["by_layer"]["container"]) == 7


def test_list_modules_layer_filter_and_bad_layer(registry):
    container = registry.call("list_modules", {"layer": "container"}, session_id="s")
    assert container.ok is True
    assert {m["layer"] for m in container.payload["modules"]} == {"container"}
    assert container.payload["count"] == 7

    bad = registry.call("list_modules", {"layer": "nope"}, session_id="s")
    assert bad.ok is False
    assert bad.error_code == ERR_INVALID_ARGUMENTS


def test_list_metric_catalog_reads_the_metric_registry(registry):
    result = registry.call("list_metric_catalog", {}, session_id="s")
    assert result.ok is True
    metrics = result.payload["metrics"]
    assert len(metrics) == len(metrics_contract.list_metrics()) == 11
    assert ([m["key"] for m in metrics]
            == [m["key"] for m in metrics_contract.list_metrics()])
    assert result.payload["home"] == [m["key"] for m in metrics if m["on_home"]]
    assert set(result.payload["home"]) == set(metrics_contract.HOME_DEFAULT)


def test_list_metric_catalog_filters(registry):
    home = registry.call("list_metric_catalog", {"home_only": True}, session_id="s")
    assert home.ok is True
    assert home.payload["count"] == len(metrics_contract.HOME_DEFAULT)
    assert all(m["on_home"] for m in home.payload["metrics"])

    sample = registry.call("list_metric_catalog", {"group": "sample"}, session_id="s")
    assert sample.ok is True
    assert {m["group"] for m in sample.payload["metrics"]} == {"sample"}

    bad = registry.call("list_metric_catalog", {"group": "nope"}, session_id="s")
    assert bad.ok is False
    assert bad.error_code == ERR_INVALID_ARGUMENTS


def test_list_canonical_fields_serves_the_frozen_vocabulary(registry):
    result = registry.call("list_canonical_fields", {}, session_id="s")
    assert result.ok is True
    groups = result.payload["fields"]
    for group in ("object_ref", "measure", "time", "state", "content", "reference"):
        assert groups.get(group), f"missing canonical group {group}"
    flat = set(result.payload["flat"])
    for field in ("style_code", "status", "evidence", "title", "metric_value"):
        assert field in flat
    assert result.payload["display_only"] == "display_only"
    # Sourced from the 0.2 freeze, not from the retired 0.1 list.
    assert "STANDARD_FIELD_VOCAB" in result.payload["source"]


# --------------------------------------------------------------------------- #
# 3. preview_descriptor: useful, and strictly free of side effects
# --------------------------------------------------------------------------- #
def test_preview_has_no_side_effects(registry):
    result = registry.call("preview_descriptor", {"descriptor": good_descriptor()},
                           session_id="sess-test")
    assert result.ok is True, result.to_dict()
    assert result.payload["valid"] is True
    assert result.payload["errors"] == []
    assert result.payload["persisted"] is False
    assert "<div" in result.payload["rendered_html"]
    assert "进汇总" in result.payload["rendered_html"]
    # Nothing was created, and the only audit line is the preview itself.
    assert registry.context.store.all_versions() == []
    assert [e.tool for e in registry.context.audit.entries] == ["preview_descriptor"]


def test_preview_is_repeatable_and_still_writes_nothing(registry):
    first = registry.call("preview_descriptor", {"descriptor": good_descriptor()})
    second = registry.call("preview_descriptor", {"descriptor": good_descriptor()})
    assert first.payload["rendered_html"] == second.payload["rendered_html"]
    assert registry.context.store.all_versions() == []
    assert len(registry.context.audit.entries) == 2


def test_preview_does_not_touch_an_existing_draft(registry):
    plugin_id = seed(registry)
    before = [v.version_no for v in registry.context.store.versions(plugin_id)]
    result = registry.call("preview_descriptor",
                           {"descriptor": good_descriptor(name="改过的名字")})
    assert result.ok is True
    assert [v.version_no for v in registry.context.store.versions(plugin_id)] == before
    assert registry.context.store.latest(plugin_id).name == "生产打样看板"


def test_preview_reports_every_contract_problem_with_a_code_prefix(registry):
    broken = good_descriptor()
    broken["modules"] = [{"key": "x", "type": "not_a_module"}]
    result = registry.call("preview_descriptor", {"descriptor": broken})
    assert result.ok is True
    assert result.payload["valid"] is False
    assert result.payload["errors"]
    codes = {e.split("]")[0].strip("[") for e in result.payload["errors"]}
    assert codes <= set(descriptor_contract.REJECT_CODES)
    assert "MODULE_NOT_WHITELISTED" in codes
    assert registry.context.store.all_versions() == []


def test_preview_resolves_metric_bindings_against_the_catalog(registry):
    descriptor = good_descriptor()
    descriptor["metrics"] = [
        {"metric_key": "funnel.sample_done", "mode": "display"},
        {"metric_key": "dept.PRODUCTION.not_a_real_metric", "mode": "display"},
    ]
    result = registry.call("preview_descriptor", {"descriptor": descriptor})
    preview = result.payload["aggregates_preview"]
    assert preview["count"] == 1
    assert preview["resolved"][0]["metric_key"] == "funnel.sample_done"
    assert preview["resolved"][0]["spec"]["unit"] == "款"
    assert preview["unresolved"] == ["dept.PRODUCTION.not_a_real_metric"]


def test_preview_rejects_a_non_object_descriptor(registry):
    result = registry.call("preview_descriptor", {"descriptor": "not an object"})
    assert result.ok is False
    assert result.error_code == ERR_INVALID_ARGUMENTS


# --------------------------------------------------------------------------- #
# 4. Draft writes: append-only versions
# --------------------------------------------------------------------------- #
def test_create_draft_plugin_starts_at_version_one(registry):
    result = registry.call(
        "create_draft_plugin",
        {"plugin_id": "prod-sample-board", "org_unit_code": "PRODUCTION",
         "name": "生产打样看板", "descriptor": good_descriptor()},
        session_id="sess-test",
    )
    assert result.ok is True, result.to_dict()
    assert result.payload["version_no"] == 1
    assert result.payload["status"] == "draft"
    assert result.payload["valid"] is True
    assert result.payload["errors"] == []

    stored = registry.context.store.latest("prod-sample-board")
    assert stored.version_no == 1
    assert stored.status == "draft"
    assert stored.created_by == ACTOR_AGENT


def test_create_draft_plugin_rejects_a_duplicate_slug(registry):
    seed(registry)
    result = registry.call(
        "create_draft_plugin",
        {"plugin_id": "prod-sample-board", "org_unit_code": "PRODUCTION",
         "name": "重复", "descriptor": good_descriptor()},
    )
    assert result.ok is False
    assert result.error_code == "PLUGIN_EXISTS"
    assert len(registry.context.store.versions("prod-sample-board")) == 1


def test_each_write_appends_a_new_version(registry):
    plugin_id = seed(registry)
    registry.call("patch_draft_module",
                  {"plugin_id": plugin_id, "module": {"key": "m2", "type": "divider"}})
    registry.call("set_field_mapping",
                  {"plugin_id": plugin_id, "field_key": "note", "map_to": "note",
                   "type": "text", "label": "备注", "aggregatable": False,
                   "example": "x"})
    registry.call("set_status_map",
                  {"plugin_id": plugin_id, "status_map": {"待办": "planned"}})
    versions = registry.context.store.versions(plugin_id)
    assert [v.version_no for v in versions] == [1, 2, 3, 4]
    assert all(v.status == "draft" for v in versions)
    # v1 keeps its own descriptor: versions are snapshots, not views.
    assert [f["key"] for f in versions[0].descriptor["fields"]] == [
        "title", "status", "evidence"]


def test_patch_draft_module_appends_and_replaces(registry):
    plugin_id = seed(registry)
    appended = registry.call(
        "patch_draft_module",
        {"plugin_id": plugin_id, "module": {"key": "extra", "type": "divider"}},
    )
    assert appended.ok is True
    assert appended.payload["action"] == "appended"
    head = registry.context.store.latest(plugin_id)
    assert [m["key"] for m in head.descriptor["modules"]] == ["row", "note", "extra"]

    replaced = registry.call(
        "patch_draft_module",
        {"plugin_id": plugin_id, "module": {"key": "row", "type": "divider"}},
    )
    assert replaced.payload["action"] == "replaced"
    head = registry.context.store.latest(plugin_id)
    assert head.descriptor["modules"][0] == {"key": "row", "type": "divider"}
    assert len(head.descriptor["modules"]) == 3


def test_set_field_mapping_creates_then_updates_one_field(registry):
    plugin_id = seed(registry)
    created = registry.call(
        "set_field_mapping",
        {"plugin_id": plugin_id, "field_key": "note", "map_to": "note",
         "type": "text", "label": "备注", "aggregatable": False, "example": "x"},
    )
    assert created.ok is True
    assert created.payload["action"] == "created"

    updated = registry.call(
        "set_field_mapping",
        {"plugin_id": plugin_id, "field_key": "note", "map_to": "display_only"},
    )
    assert updated.payload["action"] == "updated"
    fields = {f["key"]: f
              for f in registry.context.store.latest(plugin_id).descriptor["fields"]}
    assert fields["note"]["map_to"] == "display_only"
    # display_only implies not aggregatable, so the writer need not repeat it.
    assert fields["note"]["aggregatable"] is False
    assert len(registry.context.store.latest(plugin_id).descriptor["fields"]) == 4


def test_set_status_map_merges_rather_than_replaces(registry):
    plugin_id = seed(registry)
    result = registry.call(
        "set_status_map",
        {"plugin_id": plugin_id, "status_map": {"待办": "planned", "生产": "done"}},
    )
    assert result.ok is True
    assert result.payload["status_map"] == {"生产": "done", "待办": "planned"}
    assert registry.context.store.latest(plugin_id).descriptor["status_map"] == {
        "生产": "done", "待办": "planned"}


def test_writes_still_run_the_contract_validator_and_report_every_problem(registry):
    plugin_id = seed(registry)
    result = registry.call(
        "patch_draft_module",
        # note_block without its required `params.text`.
        {"plugin_id": plugin_id, "module": {"key": "blank", "type": "note_block"}},
    )
    assert result.ok is True
    assert result.payload["valid"] is False
    assert result.payload["errors"]
    assert all(e.startswith("[") for e in result.payload["errors"])
    # A draft may be temporarily invalid -- that is what a draft is for. The
    # version is still written so the AI can iterate on it.
    assert result.payload["version_no"] == 2


def test_write_tools_reject_missing_arguments(registry):
    for tool, arguments in (
        ("create_draft_plugin", {"plugin_id": "abc"}),
        ("patch_draft_module", {"plugin_id": "abc"}),
        ("set_field_mapping", {"plugin_id": "abc"}),
        ("set_status_map", {"plugin_id": "abc", "status_map": {}}),
        ("get_draft", {}),
    ):
        result = registry.call(tool, arguments)
        assert result.ok is False, tool
        assert result.error_code == ERR_INVALID_ARGUMENTS, tool
    assert registry.context.store.all_versions() == []


def test_writing_to_an_unknown_plugin_is_refused_without_creating_one(registry):
    result = registry.call(
        "patch_draft_module",
        {"plugin_id": "ghost", "module": {"key": "m", "type": "divider"}},
    )
    assert result.ok is False
    assert result.error_code == "PLUGIN_NOT_FOUND"
    assert registry.context.store.versions("ghost") == []


# --------------------------------------------------------------------------- #
# 5. The approval boundary: an AI can never submit its own work
# --------------------------------------------------------------------------- #
def test_agent_capability_never_carries_plugin_approve():
    capability = issue_agent_capability("sess-test")
    assert capability.can(CAP_PLUGIN_READ) is True
    assert capability.can(CAP_PLUGIN_WRITE_DRAFT) is True
    assert capability.can(CAP_PLUGIN_APPROVE) is False
    with pytest.raises(CapabilityDenied):
        capability.require(CAP_PLUGIN_APPROVE)
    with pytest.raises(CapabilityDenied):
        issue_agent_capability("sess-test", extra=(CAP_PLUGIN_APPROVE,))


def test_submit_for_review_is_refused_for_an_agent_even_with_human_confirmed(registry):
    plugin_id = seed(registry)
    result = registry.call(
        "submit_for_review",
        {"plugin_id": plugin_id, "human_confirmed": True},
        session_id="sess-test",
    )
    assert result.ok is False
    assert result.error_code == ERR_CAPABILITY_DENIED
    assert result.payload["required_capability"] == CAP_PLUGIN_APPROVE
    # The refusal had no side effect: still a writable draft.
    head = registry.context.store.latest(plugin_id)
    assert head.status == "draft"
    assert head.submitted_at is None
    assert len(registry.context.store.versions(plugin_id)) == 1


def test_submit_for_review_without_human_confirmation_is_refused(human_registry):
    plugin_id = seed(human_registry)
    result = human_registry.call("submit_for_review", {"plugin_id": plugin_id})
    assert result.ok is False
    assert result.error_code == ERR_HUMAN_CONFIRMATION_REQUIRED
    assert human_registry.context.store.latest(plugin_id).status == "draft"


def test_a_human_can_submit_a_draft_for_review(human_registry):
    plugin_id = seed(human_registry)
    result = human_registry.call(
        "submit_for_review",
        {"plugin_id": plugin_id, "human_confirmed": True, "review_note": "请评审"},
        session_id="sess-human",
    )
    assert result.ok is True, result.to_dict()
    assert result.payload["status"] == "pending"
    assert result.payload["submitted_by"] == "alice"
    head = human_registry.context.store.latest(plugin_id)
    assert head.status == "pending"
    assert head.review_note == "请评审"


def test_pending_version_is_frozen_and_next_write_opens_a_new_draft(human_registry):
    plugin_id = seed(human_registry)
    human_registry.call("submit_for_review",
                        {"plugin_id": plugin_id, "human_confirmed": True})
    pending = human_registry.context.store.latest(plugin_id)
    assert pending.status == "pending"
    submitted = json.dumps(pending.descriptor, sort_keys=True, ensure_ascii=False)

    # The AI keeps working after the human submitted: it must not touch v1.
    human_registry.context.capability = issue_agent_capability("sess-test")
    result = human_registry.call(
        "set_status_map",
        {"plugin_id": plugin_id, "status_map": {"待办": "planned"}},
        session_id="sess-test",
    )
    assert result.ok is True
    assert result.payload["version_no"] == 2
    assert result.payload["status"] == "draft"

    versions = human_registry.context.store.versions(plugin_id)
    assert [(v.version_no, v.status) for v in versions] == [(1, "pending"), (2, "draft")]
    assert json.dumps(versions[0].descriptor, sort_keys=True,
                      ensure_ascii=False) == submitted


def test_submitting_twice_is_refused(human_registry):
    plugin_id = seed(human_registry)
    first = human_registry.call("submit_for_review",
                                {"plugin_id": plugin_id, "human_confirmed": True})
    assert first.ok is True
    second = human_registry.call("submit_for_review",
                                 {"plugin_id": plugin_id, "human_confirmed": True})
    assert second.ok is False
    assert second.error_code == "ALREADY_SUBMITTED"
    assert len(human_registry.context.store.versions(plugin_id)) == 1


# --------------------------------------------------------------------------- #
# 6. Reads over stored drafts
# --------------------------------------------------------------------------- #
def test_get_draft_reports_writability_and_history(registry):
    plugin_id = seed(registry)
    registry.call("set_status_map", {"plugin_id": plugin_id,
                                     "status_map": {"待办": "planned"}})
    result = registry.call("get_draft", {"plugin_id": plugin_id})
    assert result.ok is True
    assert result.payload["status"] == "draft"
    assert result.payload["writable"] is True
    assert result.payload["valid"] is True
    assert result.payload["version_no"] == 2
    assert [v["version_no"] for v in result.payload["versions"]] == [1, 2]


def test_get_plugin_can_read_one_version_or_the_head(registry):
    plugin_id = seed(registry)
    registry.call("set_status_map", {"plugin_id": plugin_id,
                                     "status_map": {"待办": "planned"}})

    head = registry.call("get_plugin", {"plugin_id": plugin_id})
    assert head.ok is True
    assert head.payload["version_no"] == 2

    first = registry.call("get_plugin", {"plugin_id": plugin_id, "version_no": 1})
    assert first.ok is True
    assert first.payload["version_no"] == 1
    assert "待办" not in (first.payload["descriptor"]["status_map"] or {})


def test_get_plugin_reports_unknown_plugins(registry):
    result = registry.call("get_plugin", {"plugin_id": "ghost"})
    assert result.ok is False
    assert result.error_code == "PLUGIN_NOT_FOUND"


def test_list_plugins_returns_one_row_per_plugin(registry):
    seed(registry, plugin_id="alpha")
    seed(registry, plugin_id="beta", descriptor=good_descriptor(interface_id="beta"))
    registry.call("set_status_map", {"plugin_id": "beta",
                                     "status_map": {"待办": "planned"}})
    result = registry.call("list_plugins", {})
    assert result.ok is True
    assert result.payload["count"] == 2
    assert [row["plugin_id"] for row in result.payload["plugins"]] == ["alpha", "beta"]
    assert result.payload["plugins"][1]["version_no"] == 2

    assert registry.call("list_plugins", {"status": "draft"}).payload["count"] == 2
    assert registry.call("list_plugins", {"status": "pending"}).payload["count"] == 0


# --------------------------------------------------------------------------- #
# 7. Audit trail: every call, including the refusals
# --------------------------------------------------------------------------- #
def test_audit_records_successes_and_refusals_with_the_actor(registry, human_registry):
    plugin_id = seed(registry)
    registry.call("submit_for_review", {"plugin_id": plugin_id,
                                        "human_confirmed": True},
                  session_id="sess-test")

    entries = registry.context.audit.entries
    assert [e.tool for e in entries] == ["create_draft_plugin", "submit_for_review"]
    created, refused = entries
    assert created.ok is True
    assert created.actor == ACTOR_AGENT
    assert created.session_id == "sess-test"
    assert created.version_no == 1
    # The denied attempt is the line a reviewer most needs to see.
    assert refused.ok is False
    assert refused.error_code == ERR_CAPABILITY_DENIED
    assert refused.actor == ACTOR_AGENT

    human_registry.call("submit_for_review",
                        {"plugin_id": seed(human_registry), "human_confirmed": True})
    assert human_registry.context.audit.entries[-1].actor == ACTOR_HUMAN
    assert human_registry.context.audit.entries[-1].subject == "alice"


def test_audit_entries_are_json_serialisable(registry):
    seed(registry)
    for entry in registry.context.audit.entries:
        json.dumps(entry.to_dict(), ensure_ascii=False)


def test_unknown_tool_is_rejected_without_touching_the_store(registry):
    result = registry.call("approve_plugin", {"plugin_id": "x"})
    assert result.ok is False
    assert result.error_code == ERR_INVALID_ARGUMENTS
    assert registry.context.store.all_versions() == []


# --------------------------------------------------------------------------- #
# 8. The store seam: a different store implementation changes nothing
# --------------------------------------------------------------------------- #
def test_the_write_path_only_depends_on_the_store_interface():
    store = FakeStore()
    registry = build_registry(store=store,
                              capability=issue_agent_capability("sess-test"))
    plugin_id = seed(registry)
    registry.call("set_status_map", {"plugin_id": plugin_id,
                                     "status_map": {"待办": "planned"}})
    assert store.calls == [("create_draft", plugin_id),
                           ("append_draft_version", plugin_id)]
    assert registry.call("get_draft",
                         {"plugin_id": plugin_id}).payload["version_no"] == 2


def test_a_refused_call_writes_nothing_through_the_store():
    store = FakeStore()
    registry = build_registry(store=store,
                              capability=issue_agent_capability("sess-test"))
    plugin_id = seed(registry)
    store.calls.clear()
    result = registry.call("submit_for_review",
                           {"plugin_id": plugin_id, "human_confirmed": True})
    assert result.ok is False
    assert store.calls == []


def test_build_registry_defaults_to_an_in_memory_store_and_fresh_audit():
    first = build_registry()
    second = build_registry()
    assert isinstance(first.context.store, MemoryDraftStore)
    assert first.context.store is not second.context.store
    assert first.context.audit is not second.context.audit
    assert first.context.store.all_versions() == []
