"""The tool loop: call the model, run its tool calls, repeat until text-only.

Mutates the caller's ``messages`` list in place (assistant turns and tool
results are appended) so the session owns the full transcript afterwards.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from chief.agent.tools import ToolDispatcher
from chief.provider.base import Completion, Provider, TextDelta, ToolCall, Usage

OnDelta = Callable[[str], Awaitable[None]]

# Backstop against a model that never stops calling tools; generous because
# real multi-step work legitimately chains many calls.
MAX_ITERATIONS = 25


@dataclass(frozen=True)
class TurnResult:
    """Outcome of one full turn: final assistant text plus summed usage.

    ``notice`` carries an out-of-band owner message (e.g. a budget warning)
    the adapter should deliver after the reply.
    """

    text: str
    usage: Usage = Usage()
    notice: str | None = None


async def run_turn(
    *,
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: ToolDispatcher,
    on_delta: OnDelta,
    max_iterations: int = MAX_ITERATIONS,
) -> TurnResult:
    """Drive the model until it answers with text and no tool calls."""
    usage = Usage()
    for _ in range(max_iterations):
        completion = await _stream_once(provider, model, messages, tools, on_delta)
        usage = usage + completion.usage
        messages.append(_assistant_message(completion))
        if not completion.tool_calls:
            return TurnResult(text=completion.text, usage=usage)
        for call in completion.tool_calls:
            result = await tools.dispatch(call)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )
    return TurnResult(
        text="error: turn exceeded the tool-call iteration limit", usage=usage
    )


async def _stream_once(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: ToolDispatcher,
    on_delta: OnDelta,
) -> Completion:
    completion: Completion | None = None
    stream = provider.stream(model=model, messages=messages, tools=tools.specs())
    async for event in stream:
        if isinstance(event, TextDelta):
            await on_delta(event.text)
        else:
            completion = event
    if completion is None:
        raise RuntimeError("provider stream ended without a Completion")
    return completion


def _assistant_message(completion: Completion) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.text or None,
    }
    if completion.tool_calls:
        message["tool_calls"] = [_wire_call(call) for call in completion.tool_calls]
    return message


def _wire_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }
