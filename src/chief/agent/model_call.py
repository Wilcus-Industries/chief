"""One model call inside the tool loop: stream it, then shape its transcript
message. Split out of ``loop.py`` (length cap); the loop owns iteration,
tool dispatch, and injections."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from chief.provider.base import (
    Completion,
    Provider,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    for_model,
)
from chief.tools import ToolDispatcher

OnDelta = Callable[[str], Awaitable[None]]


async def stream_once(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: ToolDispatcher,
    on_delta: OnDelta,
    on_reasoning: OnDelta | None,
) -> Completion:
    """Stream one call: text to ``on_delta``, reasoning to ``on_reasoning``.
    The model is sent ``messages`` minus display-only keys (stored reasoning)."""
    completion: Completion | None = None
    stream = provider.stream(
        model=model, messages=for_model(messages), tools=tools.specs()
    )
    async for event in stream:
        if isinstance(event, TextDelta):
            await on_delta(event.text)
        elif isinstance(event, ReasoningDelta):
            if on_reasoning is not None:
                await on_reasoning(event.text)
        else:
            completion = event
    if completion is None:
        raise RuntimeError("provider stream ended without a Completion")
    return completion


def assistant_message(completion: Completion) -> dict[str, Any]:
    """The transcript message for a completion; non-blank reasoning rides along
    under ``reasoning`` for display."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.text or None,
    }
    if completion.reasoning.strip():
        message["reasoning"] = completion.reasoning
    if completion.tool_calls:
        message["tool_calls"] = [_wire_call(call) for call in completion.tool_calls]
    return message


def _wire_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }
