"""The tool loop: streaming, dispatch, transcript building, iteration cap."""

from typing import Any

from chief.agent.loop import run_turn
from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import Completion, TextDelta, ToolCall, ToolSpec, Usage

from .fakes import FakeProvider, text_turn

ECHO_SPEC = ToolSpec(
    name="echo",
    description="Echo the input back.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)


def echo_registry() -> ToolRegistry:
    async def echo(text: str) -> str:
        return f"echo: {text}"

    registry = ToolRegistry()
    registry.register(Tool(spec=ECHO_SPEC, handler=echo))
    return registry


class DeltaSink:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def __call__(self, text: str) -> None:
        self.chunks.append(text)


async def test_text_only_turn_streams_and_returns() -> None:
    provider = FakeProvider([text_turn("hello there")])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    sink = DeltaSink()
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=ToolRegistry(),
        on_delta=sink,
    )
    assert result.text == "hello there"
    assert "".join(sink.chunks) == "hello there"
    assert messages[-1] == {"role": "assistant", "content": "hello there"}


async def test_tool_call_turn_dispatches_and_loops() -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(call,), usage=Usage(cost=0.01))],
            [TextDelta("done"), Completion(text="done", usage=Usage(cost=0.02))],
        ]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "echo hi"}]
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=echo_registry(),
        on_delta=DeltaSink(),
    )
    assert result.text == "done"
    assert result.usage.cost == 0.03
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_message = messages[2]
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "echo: hi",
    }
    # The second model call must see the tool result.
    assert provider.calls[1][-1] == tool_message


async def test_unknown_tool_result_feeds_back_to_the_model() -> None:
    call = ToolCall(id="c1", name="missing", arguments={})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("recovered")]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=ToolRegistry(),
        on_delta=DeltaSink(),
    )
    assert result.text == "recovered"
    assert messages[2]["content"] == "error: unknown tool 'missing'"


async def test_iteration_cap_stops_a_tool_loop_runaway() -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "again"})
    looping: list[list[Any]] = [
        [Completion(text="", tool_calls=(call,))] for _ in range(3)
    ]
    provider = FakeProvider(looping)
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=[{"role": "user", "content": "go"}],
        tools=echo_registry(),
        on_delta=DeltaSink(),
        max_iterations=3,
    )
    assert "iteration limit" in result.text
    assert len(provider.calls) == 3
