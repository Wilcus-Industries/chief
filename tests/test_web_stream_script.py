"""The web client's transcript stream (``script_stream.py``): its pure helpers
run under node; the page wiring is checked as text. Skipped without node."""

from typing import Any

from chief.web.pages import CHAT_PAGE
from chief.web.script_stream import STREAM_PURE_SCRIPT

from .node_js import needs_node, run_js

pytestmark = needs_node


def call(expr: str) -> Any:
    return run_js(STREAM_PURE_SCRIPT + f"process.stdout.write(JSON.stringify({expr}));")


def test_a_collapsed_group_shows_only_the_latest_tool_name() -> None:
    assert call('groupLabel(["bash"])') == "bash"
    assert call('groupLabel(["bash", "read_file", "grep"])') == "3 tool calls · grep"


def test_a_group_with_no_tool_calls_is_a_thinking_block() -> None:
    assert call("groupLabel([])") == "thinking"


def test_near_bottom_tolerates_a_few_pixels_of_slack() -> None:
    assert call("nearBottom({scrollHeight: 1000, scrollTop: 560, clientHeight: 400})")
    assert not call(
        "nearBottom({scrollHeight: 1000, scrollTop: 100, clientHeight: 400})"
    )


def test_a_final_completes_a_reply_joined_mid_stream() -> None:
    # switched in (or reconnected) mid-reply: only the tail streamed here
    assert call('finalText("lo world", "hello world")') == "hello world"


def test_a_final_keeps_a_bubble_spanning_several_model_calls() -> None:
    # tool frames off: one bubble holds every call; the final carries the last
    assert call('finalText("first. second", "second")') == "first. second"
    assert call('finalText("", "only")') == "only"


def test_client_rows_past_a_shrunken_transcript_are_dropped() -> None:
    rows = "[{anchor: 0}, {anchor: 3}, {anchor: 9}]"
    assert call(f"liveEphemeral({rows}, 3)") == [{"anchor": 0}, {"anchor": 3}]


def test_page_has_the_jump_to_latest_button() -> None:
    assert 'id="jump"' in CHAT_PAGE
