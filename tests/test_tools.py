"""Tool registry: registration and dispatch, including every failure path."""

import pytest

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolCall, ToolSpec

ECHO_SPEC = ToolSpec(
    name="echo",
    description="Echo the input back.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


async def _echo(text: str) -> str:
    return f"echo: {text}"


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(spec=ECHO_SPEC, handler=_echo))
    return registry


async def test_dispatch_runs_the_handler() -> None:
    registry = make_registry()
    result = await registry.dispatch(
        ToolCall(id="1", name="echo", arguments={"text": "hi"})
    )
    assert result == "echo: hi"


async def test_dispatch_unknown_tool_returns_error_string() -> None:
    registry = make_registry()
    result = await registry.dispatch(ToolCall(id="1", name="nope", arguments={}))
    assert result == "error: unknown tool 'nope'"


async def test_dispatch_bad_arguments_returns_error_string() -> None:
    registry = make_registry()
    result = await registry.dispatch(
        ToolCall(id="1", name="echo", arguments={"wrong": "kwarg"})
    )
    assert result.startswith("error: bad arguments for 'echo'")


async def test_dispatch_handler_crash_returns_error_string() -> None:
    async def boom(text: str) -> str:
        raise ValueError("kaboom")

    registry = ToolRegistry()
    registry.register(Tool(spec=ECHO_SPEC, handler=boom))
    result = await registry.dispatch(
        ToolCall(id="1", name="echo", arguments={"text": "x"})
    )
    assert result == "error: tool 'echo' failed: kaboom"


def test_duplicate_registration_raises() -> None:
    registry = make_registry()
    with pytest.raises(ValueError, match="duplicate tool name: echo"):
        registry.register(Tool(spec=ECHO_SPEC, handler=_echo))


def test_specs_lists_registered_tools() -> None:
    registry = make_registry()
    assert registry.specs() == [ECHO_SPEC]
