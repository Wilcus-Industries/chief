"""Pure view helpers: turn stored wire messages into display rows."""

import json
from typing import Any


def _content_text(content: Any) -> str:
    """The human-readable text of a wire message's content.

    Content is either a plain string or a list of typed blocks (text + tool
    use/result); only the text blocks are shown, the rest is machinery.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def render_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map stored wire messages to display rows the UI renders, in order.

    A user/assistant message with text becomes an ``{role: owner|chief,
    text}`` row. Each tool call an assistant message made becomes its own
    ``{role: tool, call_id, name}`` row, collapsed — no args or result, those
    are fetched lazily (see :func:`find_tool_call`) so the payload's size
    tracks message count, not tool-output size. System prompts and bare
    tool-result messages carry nothing to show here and drop out.
    """
    rows: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _content_text(message.get("content")).strip()
        if text:
            rows.append(
                {"role": "owner" if role == "user" else "chief", "text": text}
            )
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            rows.append(
                {
                    "role": "tool",
                    "call_id": call.get("id", ""),
                    "name": function.get("name", ""),
                }
            )
    return rows


def find_tool_call(
    messages: list[dict[str, Any]], call_id: str
) -> tuple[str, dict[str, Any], str | None] | None:
    """One tool call's name, parsed args, and result from a stored transcript.

    The result is ``None`` when the call landed but its result hasn't (a turn
    still running its tool loop, or a crash-interrupted call not yet
    repaired) — the lazy endpoint reports that as pending. Returns ``None``
    outright when the call id is absent altogether: either the turn hasn't
    reached it yet, or an old call was folded away by compaction.
    """
    name: str | None = None
    args: dict[str, Any] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("id") == call_id:
                function = call.get("function", {})
                name = function.get("name", "")
                args = _parse_args(function.get("arguments"))
    if name is None:
        return None
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return name, args, str(message.get("content", ""))
    return name, args, None


def tool_call_response(
    messages: list[dict[str, Any]], call_id: str, busy: bool
) -> dict[str, Any]:
    """The lazy tool-call endpoint's JSON body: ``ok`` with args + result, or
    ``pending``/``compacted`` — see :func:`find_tool_call` for which. ``busy``
    is the thread's live-session status, the only signal available once the
    call id isn't in the transcript at all.
    """
    found = find_tool_call(messages, call_id)
    if found is None:
        return {"status": "pending" if busy else "compacted"}
    name, args, result = found
    if result is None:
        return {"status": "pending"}
    return {"status": "ok", "name": name, "args": args, "result": result}


def _parse_args(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
