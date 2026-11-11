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

# Fires as each requested tool call fires, so an observer can emit a name-only
# tool tick to a watching client (#263). Independent of on_commit (which
# persists) — this one is purely for live streaming.
OnTool = Callable[[ToolCall], Awaitable[None]]

# Fires after a tool result lands (post-screen), so an observer can stream the
# result body live (inline mode). Distinct from on_tool, which fires before the
# call runs and so has no result yet.
OnToolResult = Callable[[ToolCall, str], Awaitable[None]]

# Screens one tool result before it is appended for the model. Supplied by
# the session from the post_tool hooks; see chief.hooks.posttool.
PostTool = Callable[[ToolCall, str], Awaitable[str]]

# Fires right after a message (assistant turn or tool result) is appended to
# ``messages``, so a caller can persist it immediately instead of waiting for
# the whole turn to finish — the transcript view's "pending" tool-call state
# depends on this landing in the store as it happens (#261).
OnCommit = Callable[[dict[str, Any]], Awaitable[None]]

# Pulls messages that arrived for this session mid-turn (peer messages); they
# are appended after a tool batch so the running turn sees them next step.
Drain = Callable[[], list[dict[str, Any]]]

# Fires with each model call's usage as it lands, so a caller whose turn may be
# cancelled midway (a detached subagent) still records what it spent.
OnUsage = Callable[[Usage], Awaitable[None]]

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
    on_tool: OnTool | None = None,
    on_tool_result: OnToolResult | None = None,
    drain: Drain | None = None,
    stop: Callable[[], bool] | None = None,
    on_usage: OnUsage | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> TurnResult:
    """Drive the model until it answers with text and no tool calls.

    After each tool batch, ``drain`` (if given) supplies messages to append
    before the next model call, and ``stop`` returning True ends the turn
    there with the last assistant text — a subagent that called ``finish``
    costs no further model call. Leftovers after a text-only answer are the
    caller's to pick up (the turn's final text stays intact)."""
    usage = Usage()
    seen_calls: dict[tuple[str, str], int] = {}
    last_text = ""
    for _ in range(max_iterations):
        completion = await _stream_once(provider, model, messages, tools, on_delta)
        usage = usage + completion.usage
        if on_usage is not None:
            await on_usage(completion.usage)
        last_text = completion.text or last_text
        assistant_message = _assistant_message(completion)
        messages.append(assistant_message)
        if on_commit is not None:
            await on_commit(assistant_message)
        if not completion.tool_calls:
            return TurnResult(text=completion.text, usage=usage)
        for call in completion.tool_calls:
            if on_tool is not None:
                await on_tool(call)
            key = (call.name, json.dumps(call.arguments, sort_keys=True))
            seen_calls[key] = seen_calls.get(key, 0) + 1
            if seen_calls[key] > REPEAT_LIMIT:
                result = (
                    f"error: this exact {call.name} call already ran "
                    f"{REPEAT_LIMIT} times this turn with identical arguments "
                    # "a different approach" used to sit here, which on a
                    # repeated *denial* authorised the exact workaround the
                    # gate's denial texts forbid (gate_text.py).
                    "— repeating it cannot change the result. Stop. If "
                    "something refused this call, do not look for another way "
                    "to get the same effect — tell the owner. Otherwise "
                    "rethink the approach or ask the owner for help."
                )
            else:
                result = await tools.dispatch(call)
                if post_tool is not None:
                    result = await post_tool(call, result)
            tool_message = {"role": "tool", "tool_call_id": call.id, "content": result}
            messages.append(tool_message)
            if on_commit is not None:
                await on_commit(tool_message)
            if on_tool_result is not None:
                await on_tool_result(call, result)
        for injected in drain() if drain is not None else []:
            messages.append(injected)
            if on_commit is not None:
                await on_commit(injected)
        if stop is not None and stop():
            return TurnResult(text=last_text, usage=usage)
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
