"""Pure view helpers: transcript rows and lazy tool-call lookup."""

import json
from typing import Any

from chief.web.view import find_tool_call, render_transcript


def _assistant_call(
    call_id: str, name: str, args: dict[str, object]
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def test_render_transcript_interleaves_text_and_tool_rows() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "read that file"},
        _assistant_call("c1", "read_file", {"path": "/tmp/x"}),
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
        {"role": "assistant", "content": "done reading"},
    ]
    assert render_transcript(messages) == [
        {"role": "owner", "text": "read that file"},
        {"role": "tool", "call_id": "c1", "name": "read_file"},
        {"role": "chief", "text": "done reading"},
    ]


def test_render_transcript_omits_args_and_results() -> None:
    """The row list carries no args/result bodies, however large — that's the
    lazy-load contract (#261): payload size tracks message count, not
    tool-output size."""
    huge_result = "x" * 5_000_000
    messages: list[dict[str, Any]] = [
        _assistant_call("c1", "shell", {"cmd": "cat big"}),
        {"role": "tool", "tool_call_id": "c1", "content": huge_result},
    ]
    rows = render_transcript(messages)
    assert rows == [{"role": "tool", "call_id": "c1", "name": "shell"}]


def test_find_tool_call_returns_name_args_and_result() -> None:
    messages: list[dict[str, Any]] = [
        _assistant_call("c1", "read_file", {"path": "/tmp/x"}),
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
    ]
    assert find_tool_call(messages, "c1") == (
        "read_file", {"path": "/tmp/x"}, "file contents"
    )


def test_find_tool_call_pending_when_result_missing() -> None:
    messages = [_assistant_call("c1", "shell", {"cmd": "sleep 5"})]
    name, args, result = find_tool_call(messages, "c1")  # type: ignore[misc]
    assert (name, args) == ("shell", {"cmd": "sleep 5"})
    assert result is None


def test_find_tool_call_none_when_call_id_unknown() -> None:
    messages = [_assistant_call("c1", "shell", {"cmd": "sleep 5"})]
    assert find_tool_call(messages, "nope") is None
