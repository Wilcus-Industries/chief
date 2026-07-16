"""Daemon-level integration: inbound socket frame → session → tool loop →
outbound frames, with committed DB state. Only the LLM provider is faked
(PRD #183 testing seam)."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from chief.adapters.socket import SocketAdapter
from chief.agent.manager import SessionManager
from chief.agent.tools import Tool, ToolRegistry
from chief.dispatch import Dispatcher
from chief.persistence.store import MessageStore
from chief.provider.base import Completion, ToolCall, ToolSpec

from .fakes import FakeProvider, text_turn

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


def clock_registry() -> ToolRegistry:
    async def clock() -> str:
        return "12:00"

    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(
                name="clock",
                description="Read the clock.",
                parameters={"type": "object"},
            ),
            handler=clock,
        )
    )
    return registry


async def start_daemon(
    provider: FakeProvider, store: MessageStore, socket_path: Path
) -> SocketAdapter:
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: clock_registry(),
        store=store,
        default_model="test-model",
        system_prompt="test system prompt",
        max_concurrent=4,
    )
    dispatcher = Dispatcher(manager)
    adapter = SocketAdapter(socket_path, dispatcher.handle)
    dispatcher.register(adapter)
    await adapter.start()
    return adapter


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider([text_turn("hello owner")])


@pytest.fixture
async def connection(
    provider: FakeProvider,
    store: MessageStore,
    sock_path: Path,
) -> AsyncIterator[Streams]:
    adapter = await start_daemon(provider, store, sock_path)
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    yield reader, writer
    writer.close()
    await adapter.stop()


async def ask(streams: Streams, text: str, thread: str = "t1") -> list[dict[str, Any]]:
    reader, writer = streams
    writer.write(json.dumps({"thread": thread, "text": text}).encode() + b"\n")
    await writer.drain()
    frames = []
    while line := await reader.readline():
        frame = json.loads(line)
        frames.append(frame)
        if frame["type"] == "final":
            return frames
    raise AssertionError("connection closed before a final frame")


async def test_round_trip_streams_then_finalizes(
    connection: Streams, store: MessageStore
) -> None:
    frames = await ask(connection, "hi")
    assert [f["type"] for f in frames] == ["delta", "delta", "final"]
    assert frames[-1] == {"type": "final", "thread": "cli:t1", "text": "hello owner"}
    saved = await store.load("cli:t1")
    assert saved == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello owner"},
    ]


async def test_round_trip_through_a_tool_call(
    store: MessageStore, sock_path: Path
) -> None:
    call = ToolCall(id="c1", name="clock", arguments={})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("it is 12:00")]
    )
    adapter = await start_daemon(provider, store, sock_path)
    streams = await asyncio.open_unix_connection(str(sock_path))
    try:
        frames = await ask(streams, "what time is it?")
        assert frames[-1]["text"] == "it is 12:00"
        saved = await store.load("cli:t1")
        roles = [m["role"] for m in saved]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert saved[2]["content"] == "12:00"
    finally:
        streams[1].close()
        await adapter.stop()


async def test_malformed_frame_is_dropped_and_connection_survives(
    connection: Streams,
) -> None:
    reader, writer = connection
    writer.write(b"this is not json\n")
    await writer.drain()
    frames = await ask(connection, "hi")
    assert frames[-1]["text"] == "hello owner"
