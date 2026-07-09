"""Forward chief's tool surface onto the Copilot SDK's tools/MCP shape (#80, part #72).

chief builds every session's tool surface as a single ``mcp_servers`` mapping (see
:meth:`chief.core.tasks.TaskManager._wire_owner_session`) that mixes **two kinds** of
entry, keyed by server name:

* **In-process SDK servers** — the shell, scheduler, and guest tools, built with
  claude-agent-sdk's ``create_sdk_mcp_server`` into an
  :class:`~claude_agent_sdk.McpSdkServerConfig` (``{"type": "sdk", ...}``) that carries
  a live in-process MCP ``Server`` instance. The Copilot SDK has no in-process-MCP
  concept; its analogue is a flat list of :class:`~copilot.Tool` objects registered via
  ``create_session(tools=…)``.
* **External HTTP servers** — the Google (calendar / gmail / drive / sheets) and browser
  MCP containers, built by :meth:`chief.tools.google.GoogleService.server_config` into a
  ``{"type": "http", "url": …}`` dict. That shape is already Copilot's
  :data:`~copilot.session.MCPHTTPServerConfig`, so it passes straight through to
  ``create_session(mcp_servers=…)``.

:func:`partition_mcp_servers` splits the mixed mapping into those two Copilot inputs.
:func:`sdk_server_to_tools` does the real conversion for the in-process kind: it drives
the built MCP ``Server``'s own ``list_tools`` / ``call_tool`` handlers in-process, so an
invoked Copilot tool runs chief's *actual* tool logic (e.g. ``ShellService.run``) — not
a reimplementation. No tool service is touched; the adapter works off the built configs
the backend already receives.

**Tool naming is the gate contract.** claude-agent-sdk qualifies an MCP tool as
``mcp__<server>__<tool>``, and chief's gate (:mod:`chief.gate`) keys every allowlist,
``COMMAND_TOOLS`` entry, and blacklist rule off those exact strings. So each converted
Copilot tool is named ``mcp__<server_name>__<tool_name>`` too — the custom-tool name the
model calls, and the name that arrives back at
:func:`chief.core.copilot_gate.normalize_permission_request`, match chief's vocabulary
with no requalification. (The HTTP-server tools arrive split as ``server_name`` +
``tool_name``; the gate re-joins them to the same ``mcp__…`` form.)
"""

from typing import Any, cast

from claude_agent_sdk import McpSdkServerConfig
from copilot import Tool, ToolInvocation, ToolResult, convert_mcp_call_tool_result
from copilot.session import MCPServerConfig
from mcp import types as mcp_types


def qualified_tool_name(server_name: str, tool_name: str) -> str:
    """The SDK-qualified ``mcp__<server>__<tool>`` name chief's gate keys off."""
    return f"mcp__{server_name}__{tool_name}"


async def _list_sdk_tools(config: McpSdkServerConfig) -> list[mcp_types.Tool]:
    """Enumerate an in-process MCP server's tools via its own ``list_tools`` handler."""
    handler = config["instance"].request_handlers[mcp_types.ListToolsRequest]
    result = await handler(mcp_types.ListToolsRequest(method="tools/list"))
    return cast(mcp_types.ListToolsResult, result.root).tools


def _build_copilot_tool(
    config: McpSdkServerConfig, tool_def: mcp_types.Tool
) -> Tool:
    """Wrap one in-process MCP tool as a :class:`copilot.Tool`.

    The handler dispatches back through the MCP server's ``call_tool`` handler, so the
    original chief tool logic runs in-process; its ``CallToolResult`` is converted to a
    Copilot :class:`~copilot.ToolResult` with the SDK's own
    :func:`~copilot.convert_mcp_call_tool_result`. Built as a :class:`~copilot.Tool`
    directly (the object ``@define_tool`` also produces) so chief's existing JSON-schema
    tool definitions carry over verbatim, without fabricating a Pydantic model per tool.
    """
    call_handler = config["instance"].request_handlers[mcp_types.CallToolRequest]
    bare_name = tool_def.name

    async def handler(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments or {}
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=bare_name, arguments=arguments
            ),
        )
        result = await call_handler(request)
        call_result = cast(mcp_types.CallToolResult, result.root)
        return convert_mcp_call_tool_result(call_result.model_dump(by_alias=True))

    return Tool(
        name=qualified_tool_name(config["name"], bare_name),
        description=tool_def.description or "",
        parameters=tool_def.inputSchema,
        handler=handler,
    )


async def sdk_server_to_tools(config: McpSdkServerConfig) -> list[Tool]:
    """Convert one in-process ``McpSdkServerConfig`` into a list of Copilot tools."""
    tool_defs = await _list_sdk_tools(config)
    return [_build_copilot_tool(config, td) for td in tool_defs]


async def partition_mcp_servers(
    mcp_servers: dict[str, Any] | None,
) -> tuple[list[Tool], dict[str, MCPServerConfig]]:
    """Split chief's mixed ``mcp_servers`` into Copilot ``(tools, mcp_servers)``.

    In-process (``type == "sdk"``) entries become flat Copilot tools; HTTP/SSE entries
    pass through unchanged (their shape is already Copilot's ``MCPHTTPServerConfig``).
    An entry of neither shape is passed through as an MCP server rather than dropped —
    fail-loud at the SDK boundary beats silently losing a tool surface.
    """
    tools: list[Tool] = []
    http_servers: dict[str, MCPServerConfig] = {}
    for name, config in (mcp_servers or {}).items():
        if isinstance(config, dict) and config.get("type") == "sdk":
            tools.extend(await sdk_server_to_tools(cast(McpSdkServerConfig, config)))
        else:
            http_servers[name] = cast(MCPServerConfig, config)
    return tools, http_servers
