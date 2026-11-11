"""The tool loop: streaming, dispatch, transcript building, iteration cap."""

from typing import Any

from chief.agent.loop import run_turn
from chief.provider.base import Completion, TextDelta, ToolCall, ToolSpec, Usage
from chief.tools import Tool, ToolRegistry

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


async def test_on_commit_fires_for_the_assistant_call_before_its_result() -> None:
    """The tool-call transcript view's "pending" state depends on the
    assistant's tool_calls message landing before the matching tool result —
    ``on_commit`` is the seam that lets a caller persist each as it happens."""
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(call,), usage=Usage(cost=0.01))],
            [TextDelta("done"), Completion(text="done", usage=Usage(cost=0.02))],
        ]
    )
    committed: list[dict[str, Any]] = []

    async def on_commit(message: dict[str, Any]) -> None:
        committed.append(message)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "echo hi"}]
    await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=echo_registry(),
        on_delta=DeltaSink(),
        on_commit=on_commit,
    )
    # Same order the real messages list gets them: the tool_calls turn first,
    # then its result — never the reverse, or "pending" couldn't be real.
    assert [m["role"] for m in committed] == ["assistant", "tool", "assistant"]
    assert committed[0]["tool_calls"][0]["id"] == "c1"
    assert committed[1] == {"role": "tool", "tool_call_id": "c1", "content": "echo: hi"}


async def test_identical_repeat_calls_are_refused_after_limit() -> None:
    # Perseveration breaker (audit H2): the 4th identical call is refused with
    # a course-correcting error instead of running — prod once repeated one
    # command ~30 times in two minutes.
    ran: list[str] = []

    async def echo(text: str) -> str:
        ran.append(text)
        return f"echo: {text}"

    registry = ToolRegistry()
    registry.register(Tool(spec=ECHO_SPEC, handler=echo))
    call = ToolCall(id="c1", name="echo", arguments={"text": "same"})
    rounds: list[list[Any]] = [
        [Completion(text="", tool_calls=(call,))] for _ in range(5)
    ]
    rounds.append(text_turn("gave up"))
    provider = FakeProvider(rounds)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=registry,
        on_delta=DeltaSink(),
    )
    assert result.text == "gave up"
    assert len(ran) == 3  # 4th and 5th refused
    refusals = [
        m for m in messages if m["role"] == "tool" and "already ran" in m["content"]
    ]
    assert len(refusals) == 2
    # The breaker must not authorise routing around a refusal — that is the
    # workaround the gate's own denial texts forbid (gate_text.py).
    assert "do not look for another way" in refusals[0]["content"]
    assert "different approach" not in refusals[0]["content"]


async def test_distinct_arguments_do_not_trip_the_repeat_breaker() -> None:
    calls = [
        ToolCall(id=f"c{i}", name="echo", arguments={"text": f"v{i}"})
        for i in range(5)
    ]
    rounds: list[list[Any]] = [
        [Completion(text="", tool_calls=(c,))] for c in calls
    ]
    rounds.append(text_turn("done"))
    provider = FakeProvider(rounds)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider,
        model="test-model",
        messages=messages,
        tools=echo_registry(),
        on_delta=DeltaSink(),
    )
    assert result.text == "done"
    assert not any(
        "already ran" in m["content"] for m in messages if m["role"] == "tool"
    )


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
    assert messages[2]["content"].startswith("error: unknown tool 'missing'")


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


async def test_drain_appends_injected_messages_after_the_tool_results() -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("done")]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    committed: list[dict[str, Any]] = []
    pending = [{"role": "user", "content": "[message m-1] also check x"}]

    async def commit(message: dict[str, Any]) -> None:
        committed.append(message)

    def drain() -> list[dict[str, Any]]:
        taken = list(pending)
        pending.clear()
        return taken

    result = await run_turn(
        provider=provider, model="m", messages=messages, tools=echo_registry(),
        on_delta=DeltaSink(), on_commit=commit, drain=drain,
    )
    assert result.text == "done"
    # The second model call sees the tool result, then the injected message.
    second = provider.calls[1]
    assert second[-2]["role"] == "tool"
    assert second[-1] == {"role": "user", "content": "[message m-1] also check x"}
    assert {"role": "user", "content": "[message m-1] also check x"} in committed


async def test_stop_ends_the_turn_after_the_tool_batch_without_a_model_call() -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    provider = FakeProvider([[Completion(text="wrapping up", tool_calls=(call,))]])
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    result = await run_turn(
        provider=provider, model="m", messages=messages, tools=echo_registry(),
        on_delta=DeltaSink(), stop=lambda: True,
    )
    assert result.text == "wrapping up"
    assert len(provider.calls) == 1
    assert messages[-1]["role"] == "tool"
