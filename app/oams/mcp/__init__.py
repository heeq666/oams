"""OAMS MCP server: the AI's only door into the interface generator.

Public surface (what other code may import):

    capabilities  -- the capability vocabulary and issue/check functions
    audit         -- audit entry shape and the memory store used by tests
    store         -- plugin versions, status machine, draft store
    tools         -- tool specs + ToolRegistry (all rules live here)
    live          -- three tools that call a running site over HTTP with a ticket
    server        -- thin MCP stdio adapter over ToolRegistry

The split exists so the rules can be tested, and audited, without a transport:
`server.py` is the only file that knows the `mcp` package exists.
"""
from oams.mcp.audit import (
    ACTOR_AGENT,
    ACTOR_HUMAN,
    AuditEntry,
    AuditStore,
    MemoryAuditStore,
)
from oams.mcp.capabilities import (
    AGENT_CAPABILITIES,
    CAP_PLUGIN_APPROVE,
    CAP_PLUGIN_READ,
    CAP_PLUGIN_WRITE_DRAFT,
    FORBIDDEN_FOR_AGENT,
    HUMAN_CAPABILITIES,
    KIND_AGENT,
    KIND_HUMAN,
    Capability,
    CapabilityDenied,
    ensure_human,
    issue_agent_capability,
    issue_human_capability,
)
from oams.mcp.store import (
    ALL_STATUSES,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PENDING,
    PluginVersion,
    StoreError,
    VersionFrozen,
)
from oams.mcp.live import (
    LIVE_TOOL_SPECS,
    LIVE_TOOLS_BY_NAME,
    LiveApiError,
    LiveConfigError,
    build_live_registry,
)
from oams.mcp.tools import (
    TOOL_SPECS,
    TOOLS_BY_NAME,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_registry,
)

__all__ = [
    "ACTOR_AGENT", "ACTOR_HUMAN", "AGENT_CAPABILITIES", "ALL_STATUSES",
    "AuditEntry", "AuditStore", "CAP_PLUGIN_APPROVE", "CAP_PLUGIN_READ",
    "CAP_PLUGIN_WRITE_DRAFT", "Capability", "CapabilityDenied",
    "FORBIDDEN_FOR_AGENT", "HUMAN_CAPABILITIES", "KIND_AGENT", "KIND_HUMAN",
    "MemoryAuditStore", "PluginVersion", "STATUS_ACTIVE", "STATUS_ARCHIVED",
    "STATUS_DRAFT", "STATUS_PENDING", "StoreError", "TOOL_SPECS",
    "TOOLS_BY_NAME", "ToolContext", "ToolRegistry", "ToolResult", "ToolSpec",
    "LIVE_TOOL_SPECS", "LIVE_TOOLS_BY_NAME", "LiveApiError", "LiveConfigError",
    "VersionFrozen", "build_live_registry", "build_registry", "ensure_human",
    "issue_agent_capability", "issue_human_capability",
]
