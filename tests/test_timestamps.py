import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chief.agent.timestamps import stamp


def test_stamp_prefixes_local_time_zone_and_offset(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    moment = datetime(2026, 11, 12, 22, 30, 5, tzinfo=UTC)

    assert stamp("hi", moment) == "[2026-11-12 17:30:05 EST, UTC-05:00] hi"


def test_stamp_handles_positive_offset(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    moment = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    assert stamp("x", moment) == "[2026-01-01 05:30:00 IST, UTC+05:30] x"


def test_stamp_defaults_to_now_and_keeps_text():
    result = stamp("body")

    assert result.endswith("] body")
    assert result.startswith("[")
    assert datetime.now(UTC) - timedelta(minutes=1) < datetime.now(UTC)


STAMP_PATTERN = r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \S+, UTC[+-]\d\d:\d\d\] "


@pytest.mark.timestamps
async def test_loop_stamps_tool_results() -> None:
    from chief.agent.loop import run_turn
    from chief.provider.base import Completion, ToolCall, Usage
    from tests.fakes import FakeProvider, text_turn
    from tests.test_loop import echo_registry

    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,), usage=Usage())], text_turn("ok")]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "x"}]

    async def discard(_text: str) -> None:
        return None

    await run_turn(
        provider=provider,
        model="m",
        messages=messages,
        tools=echo_registry(),
        on_delta=discard,
    )

    tool_content = messages[2]["content"]
    assert re.match(STAMP_PATTERN, tool_content)
    assert tool_content.endswith("echo: hi")
