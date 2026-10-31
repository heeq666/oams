"""stdio MCP server for the Workhub interface generator.

This file is deliberately thin: it owns *only* the MCP SDK adaptation.

    JSON-RPC  <->  mcp.server.lowlevel.Server  <->  ToolRegistry (tools.py)

Everything that decides what an AI may do lives in `tools.py` (dispatch, capability
checks, audit) and in `store.py` (version discipline). Consequences:

* the tests never start a subprocess -- they call `ToolRegistry.call` directly;
* if the `mcp` package is missing, importing this module still works and
  `tool_schemas()` / `dispatch()` stay usable, so the rules remain exercisable and
  the server reports a precise error instead of dying on import.

Wire protocol notes (MCP SDK >= 2.3):
    async def on_list_tools(ctx, params) -> types.ListToolsResult
    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult
Tool results carry a JSON text block; `isError` mirrors our ToolResult.ok so a
client can distinguish a rejection from a success without parsing the body.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from oams.mcp.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    build_registry,
)

SERVER_NAME = "oams-interface"
SERVER_VERSION = "0.1.0"

#: Instruction block a client shows to the AI before the first call.
SERVER_INSTRUCTIONS = (
    "Workhub 部门界面插件服务端。你可以：读模块注册表/指标目录/标准字段，"
    "在 draft 上创建与修改插件描述符，预览渲染结果。"
    "你不能批准任何插件：submit_for_review 只接受人类调用（插件描述符的每次上架都必须人工评审）。"
)


class McpSdkUnavailable(RuntimeError):
    """Raised only when someone actually wants to serve; imports still work."""


def _import_sdk() -> tuple[Any, Any]:
    try:
        import mcp.server.lowlevel as lowlevel
        import mcp.types as types
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise McpSdkUnavailable(
            "the 'mcp' package is not installed; run "
            "'.venv/bin/python -m pip install \"mcp>=2.3.0\"' to serve over stdio. "
            f"(import error: {error})"
        ) from error
    return lowlevel, types


def _session_id() -> str:
    return os.environ.get("OAMS_MCP_SESSION_ID") or "stdio-session"


# --------------------------------------------------------------------------- #
# SDK-free surface (what the tests and any embedder use)
# --------------------------------------------------------------------------- #
def tool_schemas(registry: ToolRegistry | None = None) -> list[dict[str, Any]]:
    """The `tools/list` payload, without needing the MCP SDK or a transport."""
    return (registry or build_registry()).schema_list()


def dispatch(name: str, arguments: dict[str, Any] | None = None,
             registry: ToolRegistry | None = None,
             session_id: str | None = None) -> dict[str, Any]:
    """Invoke one tool and return the JSON body a client would receive."""
    result = (registry or build_registry()).call(
        name, arguments, session_id=session_id or _session_id(),
    )
    return result.to_dict()


def render_result_json(result: ToolResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# SDK adapter
# --------------------------------------------------------------------------- #
def build_server(registry: ToolRegistry | None = None) -> Any:
    """Create the lowlevel MCP server bound to a registry.

    Kept as a function (not module-level state) so a test can build one per case
    with its own in-memory store and inspect the audit trail afterwards.
    """
    lowlevel, types = _import_sdk()
    registry = registry or build_registry()
    session_id = _session_id()

    async def on_list_tools(ctx: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=[
            types.Tool(name=spec["name"], description=spec["description"],
                       input_schema=spec["inputSchema"])
            for spec in registry.schema_list()
        ])

    async def on_call_tool(ctx: Any, params: Any) -> Any:
        # params.arguments is the untrusted payload from the model; the registry
        # is the only thing allowed to interpret it.
        result = registry.call(params.name, params.arguments or {}, session_id=session_id)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=render_result_json(result))],
            structured_content=result.to_dict(),
            is_error=not result.ok,
        )

    return lowlevel.Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(registry: ToolRegistry | None = None) -> None:
    """Serve until stdin closes. Requires the `mcp` package."""
    from mcp.server.stdio import stdio_server

    server = build_server(registry)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    """Console entry point used by `python -m oams.mcp.server`."""
    import asyncio
    import sys

    try:
        asyncio.run(serve_stdio())
    except McpSdkUnavailable as error:
        print(f"oams mcp: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
