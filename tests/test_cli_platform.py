"""End-to-end tests for the CLI platform stack (#131).

The central mechanism — a real unix-socket JSONL session driving a real ``TaskManager``
turn — is exercised for real: a real :class:`SocketServer`, a real :class:`CliTaskIO`, a
real :class:`CliAdapter`, a real ``TaskManager`` on ``platform="cli"``, and a real
``asyncio.open_unix_connection`` client. The ONLY fake is the LLM: ``FakeSession`` from
``test_tasks`` stands in for the Copilot session (the established model-client seam).
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.cli import CLI_LIMIT, CliAdapter, CliTaskIO
from chief.client_plane import (
    SocketServer,
    command_frame,
    user_frame,
)
from chief.core.session import Milestone
from chief.core.tasks import SessionProto, TaskManager
from test_tasks import FakeSession

Factory = Callable[..., SessionProto]


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    line = await asyncio.wait_for(reader.readline(), 5)
    frame: dict[str, object] = json.loads(line)
    return frame


async def _no(*args: Any, **kwargs: Any) -> bool:
    return False


def _seq_factory(sessions: Sequence[FakeSession]) -> Factory:
    """A session factory that hands out ``sessions`` in call order (one per thread)."""
    it = iter(sessions)

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        session = next(it)
        session.model = model
        session.resume = resume
        return session

    return factory


def _cli_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: CliTaskIO,
    *,
    factory: Factory,
) -> TaskManager:
    """A real ``platform="cli"`` engine over the CLI IO, LLM stubbed by ``factory``."""
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        platform="cli",
        concurrency=3,
        turn_timeout=1000.0,
        idle_archive_seconds=1000.0,
        compaction_idle_seconds=1000.0,
        message_limit=CLI_LIMIT,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
    )


async def _running_server(path: str) -> tuple[SocketServer, asyncio.Task[None]]:
    server = SocketServer(path)
    task = asyncio.create_task(server.run())
    await asyncio.wait_for(server.started.wait(), 5)
    return server, task


@pytest.fixture
async def cli_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Callable[[Sequence[FakeSession]], SocketServer]]:
    """Bring up a real server + CLI stack; the test supplies the fake LLM sessions."""
    built: list[tuple[SocketServer, asyncio.Task[None], TaskManager]] = []

    async def _build(sessions: Sequence[FakeSession]) -> SocketServer:
        server, task = await _running_server(str(tmp_path / "cli.sock"))
        io = CliTaskIO(server)
        manager = _cli_manager(session_factory, io, factory=_seq_factory(sessions))
        CliAdapter(server=server, engine=manager)
        built.append((server, task, manager))
        return server

    try:
        yield _build  # type: ignore[misc]
    finally:
        # Shut the engine down FIRST — it cancels the background consumers so no turn
        # touches the DB after the session_factory fixture disposes its engine.
        for server, task, manager in built:
            await manager.shutdown()
            await server.stop()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_user_frame_streams_milestones_then_reply(
    cli_stack: Callable[..., Any],
) -> None:
    # Red anchor: a user frame drives a real owner turn; the client sees the milestone
    # frame, then the reply frame, IN ORDER, each tagged {platform: cli, thread_key}.
    session = FakeSession(model="m", milestones=[Milestone(text="using Bash")])
    server = await cli_stack([session])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "hello")).encode() + b"\n")
        await writer.drain()

        milestone = await _read_frame(reader)
        assert milestone["type"] == "milestone"
        assert milestone["platform"] == "cli"
        assert milestone["thread_key"] == "cli:main"
        assert milestone["text"] == "using Bash"  # the "· " prefix is stripped

        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["platform"] == "cli"
        assert reply["thread_key"] == "cli:main"
        assert reply["text"] == "reply:hello"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_user_frame_missing_text_gets_invalid_fields(
    cli_stack: Callable[..., Any],
) -> None:
    server = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(b'{"type":"user","thread_key":"cli:main"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "invalid_fields"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_command_tasks_replies_no_active_tasks_on_empty_db(
    cli_stack: Callable[..., Any],
) -> None:
    # A real registry dispatch: /tasks over an empty DB → "No active tasks." The reply
    # comes back on the requesting connection (sender), not a broadcast.
    server = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(command_frame("cli:main", "tasks")).encode() + b"\n")
        await writer.drain()
        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["thread_key"] == "cli:main"
        assert reply["text"] == "No active tasks."
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_unknown_command_gets_unknown_command_error(
    cli_stack: Callable[..., Any],
) -> None:
    # The unknown-command error is produced in the CLI handler — the registry keeps its
    # silent-no-op contract.
    server = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(command_frame("cli:main", "bogus")).encode() + b"\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_command"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_two_threads_stay_tagged_and_interleave(
    cli_stack: Callable[..., Any],
) -> None:
    # Thread a is gated (parked mid-turn); thread b runs free. b's reply must arrive
    # while a is still parked, and every frame carries its own thread's key. Releasing
    # a's gate then lets a's reply through — tagged cli:a.
    gate = asyncio.Event()
    session_a = FakeSession(model="m", gate=gate)
    session_b = FakeSession(model="m")
    server = await cli_stack([session_a, session_b])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        # Dispatch a (parks on its gate), then b (runs to completion).
        writer.write(json.dumps(user_frame("cli:a", "one")).encode() + b"\n")
        await writer.drain()
        writer.write(json.dumps(user_frame("cli:b", "two")).encode() + b"\n")
        await writer.drain()

        # b's reply arrives first, while a is parked.
        reply_b = await _read_frame(reader)
        assert reply_b["type"] == "reply"
        assert reply_b["thread_key"] == "cli:b"
        assert reply_b["text"] == "reply:two"

        # Release a; its reply comes through tagged cli:a.
        gate.set()
        reply_a = await _read_frame(reader)
        assert reply_a["type"] == "reply"
        assert reply_a["thread_key"] == "cli:a"
        assert reply_a["text"] == "reply:one"
    finally:
        gate.set()
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_send_file_broadcasts_a_base64_file_frame() -> None:
    # CliTaskIO.send_file → a file frame whose data base64-decodes to the raw bytes.
    server = SocketServer("/unused")
    io = CliTaskIO(server)
    sent: list[dict[str, object]] = []

    async def fake_broadcast(frame: Any) -> None:
        sent.append(dict(frame))

    server.broadcast = fake_broadcast  # type: ignore[method-assign]
    await io.send_file("cli:main", "reply.md", b"long body", caption="note")

    (frame,) = sent
    assert frame["type"] == "file"
    assert frame["filename"] == "reply.md"
    assert frame["caption"] == "note"
    assert base64.b64decode(str(frame["data"])) == b"long body"


async def test_send_detects_milestone_prefix_vs_reply() -> None:
    server = SocketServer("/unused")
    io = CliTaskIO(server)
    sent: list[dict[str, object]] = []

    async def fake_broadcast(frame: Any) -> None:
        sent.append(dict(frame))

    server.broadcast = fake_broadcast  # type: ignore[method-assign]
    await io.send("cli:main", "· thinking")  # milestone: engine's "· " prefix
    await io.send("cli:main", "final answer")  # no prefix: a reply

    assert sent[0]["type"] == "milestone" and sent[0]["text"] == "thinking"
    assert sent[1]["type"] == "reply" and sent[1]["text"] == "final answer"


async def test_create_thread_mints_sequential_cli_keys() -> None:
    io = CliTaskIO(SocketServer("/unused"))
    first = await io.create_thread(like_thread_key="cli:main", title="x")
    second = await io.create_thread(like_thread_key="cli:main", title="y")
    assert (first, second) == ("cli:t1", "cli:t2")
