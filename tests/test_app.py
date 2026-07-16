"""Whole-daemon integration through build_app: the production wiring with
only the LLM provider swapped (PRD #183 testing seam)."""

import asyncio
import json
from pathlib import Path
from typing import Any

from chief.app import App, build_app
from chief.config import Config
from chief.provider.base import Completion, ToolCall

from .fakes import FakeProvider, text_turn

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    return Config(
        models={"default": "test-model"},
        db_path=tmp_path / "chief.db",
        socket_path=tmp_path / "chief.sock",
        **overrides,
    )


async def boot(config: Config, provider: FakeProvider) -> tuple[App, Streams]:
    app = await build_app(config, provider=provider)
    await app.start()
    streams = await asyncio.open_unix_connection(str(config.socket_path))
    return app, streams


async def shutdown(app: App, streams: Streams) -> None:
    streams[1].close()
    await app.stop()


def send_frame(streams: Streams, text: str, thread: str = "t1") -> None:
    streams[1].write(json.dumps({"thread": thread, "text": text}).encode() + b"\n")


async def read_finals(streams: Streams, count: int) -> list[dict[str, Any]]:
    finals: list[dict[str, Any]] = []
    while len(finals) < count:
        line = await asyncio.wait_for(streams[0].readline(), timeout=5)
        assert line, "connection closed early"
        frame = json.loads(line)
        if frame["type"] == "final":
            finals.append(frame)
    return finals


async def test_agent_creates_a_monitor_and_it_fires(tmp_path: Path) -> None:
    create = ToolCall(
        id="c1",
        name="create_monitor",
        arguments={"description": "urgent watcher", "pattern": "urgent"},
    )
    provider = FakeProvider(
        [
            [Completion(text="", tool_calls=(create,))],
            text_turn("watching for urgent messages"),
            text_turn("monitor woke me, on it"),
            text_turn("hello from t2"),
        ]
    )
    config = make_config(tmp_path, gate_approved=("create_monitor",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "watch this channel for urgent stuff", thread="t1")
        first = await read_finals(streams, 1)
        assert first[0]["text"] == "watching for urgent messages"
        monitors = await app.monitor_service.list_enabled()
        assert len(monitors) == 1
        assert monitors[0].wake_thread == "cli:t1"

        # An urgent message on another thread wakes t1 through the monitor.
        send_frame(streams, "URGENT: the roof is leaking", thread="t2")
        finals = await read_finals(streams, 2)
        by_thread = {f["thread"]: f["text"] for f in finals}
        assert by_thread["cli:t1"] == "monitor woke me, on it"
        assert by_thread["cli:t2"] == "hello from t2"
    finally:
        await shutdown(app, streams)


async def test_agent_can_list_bundled_packages(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="list_packages", arguments={})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("here they are")]
    )
    config = make_config(tmp_path, gate_approved=("list_packages",))
    app, streams = await boot(config, provider)
    try:
        send_frame(streams, "what packages can you install?", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "here they are"
        result = provider.calls[1][-1]
        assert result["role"] == "tool"
        assert "build-imessage" in result["content"]
        assert "screening" in result["content"]
    finally:
        await shutdown(app, streams)


async def test_gray_tool_raises_an_approval_card_first_answer_wins(
    tmp_path: Path,
) -> None:
    create = ToolCall(
        id="c1",
        name="create_schedule",
        arguments={
            "description": "daily checkin",
            "spec": "0 9 * * *",
            "prompt": "say hi",
        },
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(create,))], text_turn("scheduled")]
    )
    app, streams = await boot(make_config(tmp_path), provider)
    try:
        send_frame(streams, "remind me daily", thread="t1")
        card = (await read_finals(streams, 1))[0]
        assert "approve tool call create_schedule" in card["text"]
        send_frame(streams, "yes", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "scheduled"
        schedules = await app.cron_service.list_enabled()
        assert len(schedules) == 1
        assert schedules[0].spec == "0 9 * * *"
    finally:
        await shutdown(app, streams)


async def test_denied_card_blocks_the_tool(tmp_path: Path) -> None:
    create = ToolCall(
        id="c1",
        name="create_schedule",
        arguments={"description": "d", "spec": "@every 60", "prompt": "p"},
    )
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(create,))], text_turn("understood, denied")]
    )
    app, streams = await boot(make_config(tmp_path), provider)
    try:
        send_frame(streams, "remind me", thread="t1")
        await read_finals(streams, 1)  # the card
        send_frame(streams, "no", thread="t1")
        final = (await read_finals(streams, 1))[0]
        assert final["text"] == "understood, denied"
        assert await app.cron_service.list_enabled() == []
        # The model was told the gate denied it.
        denied = provider.calls[1][-1]
        assert denied["role"] == "tool"
        assert "denied by the gate" in denied["content"]
    finally:
        await shutdown(app, streams)
