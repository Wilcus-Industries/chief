"""Forwarding chief's in-process tools onto the Copilot SDK (#80, part of #72).

The central mechanism runs for real here: a genuine chief in-process tool service
(:class:`~chief.tools.guest.GuestService`, built the same way every shell / scheduler /
guest service is — ``create_sdk_mcp_server``) is converted to a Copilot tool and
**invoked**, so the assertion exercises the actual chief tool logic running under the
Copilot tool shape, not a stand-in. Only the Copilot CLI subprocess (which would host
the tool at runtime) is out of scope; the in-process call is real.
"""

import inspect
from typing import Any

from copilot import Tool, ToolInvocation, ToolResult
from copilot.session import MCPHTTPServerConfig

from chief.core.copilot_tools import (
    partition_mcp_servers,
    qualified_tool_name,
    sdk_server_to_tools,
)
from chief.tools.guest import GuestService


def _guest_service() -> tuple[GuestService, list[str]]:
    """A real GuestService whose relay records what it was handed."""
    relayed: list[str] = []

    async def relay(text: str) -> None:
        relayed.append(text)

    return GuestService(relay=relay, from_label="Dana"), relayed


async def _invoke(tool: Tool, **arguments: Any) -> ToolResult:
    """Call a converted tool's handler (the SDK handler type may be sync or async)."""
    assert tool.handler is not None
    result = tool.handler(ToolInvocation(arguments=arguments))
    return await result if inspect.isawaitable(result) else result


async def test_sdk_server_converts_to_qualified_copilot_tool() -> None:
    # AC1: a chief in-process server becomes a flat Copilot tool, SDK-qualified so the
    # gate keys off the same mcp__<server>__<tool> name it always has.
    service, _relayed = _guest_service()

    tools = await sdk_server_to_tools(service.server_config())

    assert len(tools) == 1
    (tool,) = tools
    assert isinstance(tool, Tool)
    assert tool.name == "mcp__chief_guest__leave_message"
    assert tool.name == qualified_tool_name("chief_guest", "leave_message")
    assert tool.parameters is not None
    assert "message" in tool.parameters["properties"]


async def test_converted_tool_runs_the_real_guest_relay() -> None:
    # AC1 central mechanism: invoking the converted tool runs GuestService's actual
    # leave_message logic in-process — the relay fires and the real reply comes back.
    service, relayed = _guest_service()
    (tool,) = await sdk_server_to_tools(service.server_config())

    result = await _invoke(tool, message="Please call me")

    assert relayed == ["📨 Message from Dana:\n\nPlease call me"]
    assert result.result_type == "success"
    assert "passed along" in result.text_result_for_llm


async def test_converted_tool_surfaces_error_result() -> None:
    # The chief tool's own error path (empty message) maps to a Copilot failure result.
    service, relayed = _guest_service()
    (tool,) = await sdk_server_to_tools(service.server_config())

    result = await _invoke(tool, message="   ")

    assert relayed == []  # nothing relayed on the error path
    assert result.result_type == "failure"


async def test_partition_splits_sdk_tools_from_http_servers() -> None:
    # AC1: chief's mixed mcp_servers mapping splits into Copilot's two inputs —
    # in-process servers become flat tools; the HTTP configs pass straight through.
    service, _relayed = _guest_service()
    http: MCPHTTPServerConfig = {"type": "http", "url": "http://mcp-calendar:8000"}
    mixed: dict[str, Any] = {
        service.server_name: service.server_config(),
        "chief_calendar": http,
    }

    tools, http_servers = await partition_mcp_servers(mixed)

    assert [t.name for t in tools] == ["mcp__chief_guest__leave_message"]
    assert http_servers == {"chief_calendar": http}
    assert http_servers["chief_calendar"] is http  # passed through unchanged


async def test_partition_none_returns_empty() -> None:
    tools, http_servers = await partition_mcp_servers(None)
    assert tools == []
    assert http_servers == {}
