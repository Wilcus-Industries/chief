"""Forward chief's in-process tools onto the Copilot SDK's tools/MCP shape (#80, #88).

chief builds every session's tool surface as a single ``mcp_servers`` mapping (see
:meth:`chief.core.tasks.TaskManager._wire_owner_session`) that mixes **two kinds** of
entry, keyed by server name:

* **In-process servers** — the shell, scheduler, guest, web, routing-admin, and
  Google-account tools, built with :func:`chief.tools.inprocess.create_sdk_mcp_server`
  into an :class:`~chief.tools.inprocess.InProcessServerConfig` (``{"type": "sdk", …}``)
  carrying the list of :class:`~chief.tools.inprocess.InProcessTool` records. The
  Copilot SDK's analogue is a flat list of :class:`~copilot.Tool` objects registered via
  ``create_session(tools=…)``.
* **External HTTP servers** — the Google (calendar / gmail / drive / sheets) and browser
  MCP containers, built by :meth:`chief.tools.google.GoogleService.server_config` into a
  ``{"type": "http", "url": …}`` dict. That shape is already Copilot's
  :data:`~copilot.session.MCPHTTPServerConfig`, so it passes straight through to
  ``create_session(mcp_servers=…)``.

:func:`partition_mcp_servers` splits the mixed mapping into those two Copilot inputs.
:func:`sdk_server_to_tools` does the conversion for the in-process kind: each
:class:`InProcessTool`'s handler runs chief's *actual* tool logic (e.g.
``ShellService.run``) in-process — no ``mcp.Server`` round-trip (#88 dropped that, and
with it the ``mcp`` transitive dependency). Its ``{"content": …, "is_error": …}`` result
is mapped onto a Copilot :class:`~copilot.ToolResult` by the SDK's own
:func:`~copilot.convert_mcp_call_tool_result` (verified against github-copilot-sdk
1.0.5: it reads ``call_result["content"]`` and ``call_result.get("isError")``).

**Tool naming is the gate contract.** chief qualifies an in-process tool as
``mcp__<server>__<tool>``, and chief's gate (:mod:`chief.gate`) keys every allowlist,
``COMMAND_TOOLS`` entry, and blacklist rule off those exact strings. So each converted
Copilot tool is named ``mcp__<server_name>__<tool_name>`` too — the custom-tool name the
model calls, and the name that arrives back at
:func:`chief.core.copilot_gate.normalize_permission_request`, match chief's vocabulary
with no requalification. (The HTTP-server tools arrive split as ``server_name`` +
``tool_name``; the gate re-joins them to the same ``mcp__…`` form.)
"""

from typing import Any, cast

from copilot import Tool, ToolInvocation, ToolResult, convert_mcp_call_tool_result
from copilot.session import MCPServerConfig

from ..tools.inprocess import InProcessServerConfig, InProcessTool, build_input_schema


def qualified_tool_name(server_name: str, tool_name: str) -> str:
    """The SDK-qualified ``mcp__<server>__<tool>`` name chief's gate keys off."""
    return f"mcp__{server_name}__{tool_name}"


def _build_copilot_tool(server_name: str, tool_def: InProcessTool) -> Tool:
    """Wrap one :class:`InProcessTool` as a :class:`copilot.Tool`.

    The handler runs the original chief tool logic in-process and maps its
    ``{"content": …, "is_error": …}`` dict onto a Copilot :class:`~copilot.ToolResult`
    via the SDK's :func:`~copilot.convert_mcp_call_tool_result` (which reads a
    camelCase ``isError``), so a chief tool error surfaces as a Copilot failure result.
    """
    handler_fn = tool_def.handler

    async def handler(invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.arguments or {}
        result = await handler_fn(arguments)
        return convert_mcp_call_tool_result(
            {
                "content": result.get("content") or [],
                "isError": bool(result.get("is_error", False)),
            }
        )

    return Tool(
        name=qualified_tool_name(server_name, tool_def.name),
        description=tool_def.description,
        parameters=build_input_schema(tool_def.input_schema),
        handler=handler,
    )


async def sdk_server_to_tools(config: InProcessServerConfig) -> list[Tool]:
    """Convert one in-process ``InProcessServerConfig`` into a list of Copilot tools."""
    name = config["name"]
    return [_build_copilot_tool(name, td) for td in config["tools"]]


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
            tools.extend(
                await sdk_server_to_tools(cast(InProcessServerConfig, config))
            )
        else:
            http_servers[name] = cast(MCPServerConfig, config)
    return tools, http_servers
