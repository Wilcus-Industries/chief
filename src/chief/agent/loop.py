"""The tool loop: call the model, run its tool calls, repeat until text-only.

Mutates the caller's ``messages`` list in place (assistant turns and tool
results are appended) so the session owns the full transcript afterwards.
Tool results pass through the optional ``post_tool`` screen before they are
appended, so a hook can tag or withhold untrusted output before the model
reads it.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from chief.provider.base import Completion, Provider, TextDelta, ToolCall, Usage
from chief.tools import ToolDispatcher

OnDelta = Callable[[str], Awaitable[None]]

# Screens one tool result before it is appended for the model. Supplied by
# the session from the post_tool hooks; see chief.hooks.posttool.
PostTool = Callable[[ToolCall, str], Awaitable[str]]

# Fires right after a message (assistant turn or tool result) is appended to
# ``messages``, so a caller can persist it immediately instead of waiting for
# the whole turn to finish — the transcript view's "pending" tool-call state
# depends on this landing in the store as it happens (#261).
OnCommit = Callable[[dict[str, Any]], Awaitable[None]]

# Backstop against a model that never stops calling tools; generous because
# real multi-step work legitimately chains many calls.
MAX_ITERATIONS = 25

# Perseveration breaker: the same tool call with identical arguments ran this
# many times in one turn — a fourth run cannot yield new information, only
# burn spend (prod once repeated an identical mkdir/git-commit pair ~30 times
# in two minutes). The call is refused with a course-correcting error instead.
REPEAT_LIMIT = 3


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
    post_tool: PostTool | None = None,
    on_commit: OnCommit | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> TurnResult:
    """Drive the model until it answers with text and no tool calls."""
    usage = Usage()
    seen_calls: dict[tuple[str, str], int] = {}
    for _ in range(max_iterations):
        completion = await _stream_once(provider, model, messages, tools, on_delta)
        usage = usage + completion.usage
        assistant_message = _assistant_message(completion)
        messages.append(assistant_message)
        if on_commit is not None:
            await on_commit(assistant_message)
        if not completion.tool_calls:
            return TurnResult(text=completion.text, usage=usage)
        for call in completion.tool_calls:
            key = (call.name, json.dumps(call.arguments, sort_keys=True))
            seen_calls[key] = seen_calls.get(key, 0) + 1
            if seen_calls[key] > REPEAT_LIMIT:
                result = (
                    f"error: this exact {call.name} call already ran "
                    f"{REPEAT_LIMIT} times this turn with identical arguments "
                    "— repeating it cannot change the result. Stop, take a "
                    "different approach, or ask the owner for help."
                )
            else:
                result = await tools.dispatch(call)
                if post_tool is not None:
                    result = await post_tool(call, result)
            tool_message = {"role": "tool", "tool_call_id": call.id, "content": result}
            messages.append(tool_message)
            if on_commit is not None:
                await on_commit(tool_message)
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
