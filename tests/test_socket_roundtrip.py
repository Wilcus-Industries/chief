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
from chief.dispatch import Dispatcher
from chief.persistence.store import MessageStore
from chief.provider.base import Completion, ToolCall, ToolSpec
from chief.tools import Tool, ToolRegistry

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


async def test_send_once_read_timeout_raises_connection_error(
    sock_path: Path,
) -> None:
    """A daemon that accepts but never replies must not hang the client."""
    from chief.socket_client import send_once

    async def silent(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    server = await asyncio.start_unix_server(silent, str(sock_path))
    try:
        with pytest.raises(ConnectionError, match="silent"):
            await send_once(str(sock_path), "t", "hi", read_timeout=0.05)
    finally:
        server.close()
        await server.wait_closed()


async def test_send_once_malformed_frame_raises_connection_error(
    sock_path: Path,
) -> None:
    """A malformed reply surfaces as ConnectionError, not a raw JSONDecodeError."""
    from chief.socket_client import send_once

    async def garbage(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(b"not json\n")
        await writer.drain()

    server = await asyncio.start_unix_server(garbage, str(sock_path))
    try:
        with pytest.raises(ConnectionError, match="malformed"):
            await send_once(str(sock_path), "t", "hi")
    finally:
        server.close()
        await server.wait_closed()


async def test_send_once_compacts_a_named_thread(
    store: MessageStore, sock_path: Path
) -> None:
    """`chief compact <thread>` path: a one-shot socket client sends /compact
    aimed at another thread, and the daemon force-compacts it in-process."""
    from chief.agent.compaction import NOTE_PREFIX, Compactor
    from chief.bus import EventBus
    from chief.classifiers import Classifier, ClassifierRegistry
    from chief.commands import CommandSet
    from chief.cron.service import CronService
    from chief.monitors.service import MonitorService
    from chief.socket_client import send_once

    from .test_compaction import FixedWindow, chat

    await store.ensure_session("+15551234567", "imessage")
    await store.append("+15551234567", chat(20))

    provider = FakeProvider([text_turn("nightly note")])
    manager = SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="test-model",
        system_prompt="s",
        max_concurrent=4,
        compactor=Compactor(provider, "m", FixedWindow(10_000_000), keep_recent=4),
    )
    dispatcher = Dispatcher(manager)
    monitors = MonitorService(
        store._factory,
        EventBus(),
        dispatcher.handle,
        Classifier(FakeProvider([]), ClassifierRegistry(Path("classifiers")), "m"),
    )
    commands = CommandSet(
        manager, monitors, CronService(store._factory, dispatcher.handle), store=store
    )
    dispatcher.set_commands(commands)
    adapter = SocketAdapter(sock_path, dispatcher.handle)
    dispatcher.register(adapter)
    await adapter.start()
    try:
        reply = await send_once(str(sock_path), "compact-job", "/compact +15551234567")
    finally:
        await adapter.stop()

    assert reply.startswith("compacted")
    persisted = await store.load("+15551234567")
    assert persisted[0]["content"].startswith(NOTE_PREFIX + "nightly note")
