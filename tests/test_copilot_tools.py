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


# ---- the chief-owned in-process seam (#88) ----------------------------------------


def test_build_input_schema_converts_simple_type_map() -> None:
    # A {param: python_type} map → an object schema with every param required.
    from chief.tools.inprocess import build_input_schema

    schema = build_input_schema({"command": str, "count": int})
    assert schema == {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["command", "count"],
    }


def test_build_input_schema_passes_full_json_schema_through() -> None:
    # A full JSON Schema object (its own type/properties/required) is returned as-is.
    from chief.tools.inprocess import build_input_schema

    full = {
        "type": "object",
        "properties": {"account": {"type": "string", "description": "x"}},
        "required": [],
    }
    assert build_input_schema(full) is full


def test_create_sdk_mcp_server_carries_tools_under_sdk_type() -> None:
    # The config discriminator is type == "sdk" (what partition_mcp_servers keys off).
    from chief.tools.inprocess import create_sdk_mcp_server, tool

    @tool("echo", "echo it", {"text": str})
    async def _echo(args: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": args["text"]}]}

    config = create_sdk_mcp_server("chief_echo", tools=[_echo])
    assert config["type"] == "sdk"
    assert config["name"] == "chief_echo"
    assert [t.name for t in config["tools"]] == ["echo"]


async def test_convert_maps_is_error_to_failure_result() -> None:
    # Pin the convert_mcp_call_tool_result contract (verified vs github-copilot-sdk
    # 1.0.5): a chief tool's is_error=True → a Copilot failure ToolResult, and the text
    # content flows through.
    from chief.tools.inprocess import create_sdk_mcp_server, tool

    @tool("boom", "always errors", {"x": str})
    async def _boom(args: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "nope"}], "is_error": True}

    (t,) = await sdk_server_to_tools(create_sdk_mcp_server("chief_x", tools=[_boom]))
    result = await _invoke(t, x="y")
    assert result.result_type == "failure"
    assert result.text_result_for_llm == "nope"
