"""End-to-end tests for the CLI platform stack (#131).

The central mechanism — a real unix-socket JSONL session driving a real ``TaskManager``
turn — is exercised for real: a real :class:`SocketServer`, a real :class:`CliTaskIO`, a
real :class:`CliAdapter`, a real ``TaskManager`` on ``platform="cli"``, and a real
``asyncio.open_unix_connection`` client. The ONLY fake is the LLM: ``FakeSession`` from
``test_tasks`` stands in for the Copilot session (the established model-client seam).

#139 adds a real :class:`Scheduler` tick over the production ``build_cli_stack`` +
``build_scheduler`` wiring, delivering a reminder onto the socket (and replaying it on
attach when detached).
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief import app
from chief.adapters.base import Attachment, Surface
from chief.adapters.cli import CLI_LIMIT, CliAdapter, CliTaskIO, ForeignPlatform
from chief.adapters.mirror import MirrorTaskIO
from chief.client_plane import (
    SocketServer,
    answer_frame,
    command_frame,
    inject_frame,
    list_threads_frame,
    skills_frame,
    status_frame,
    switch_frame,
    user_frame,
)
from chief.config import Settings
from chief.core.budget import (
    ACCUM_MAX,
    ACTION_PAUSE,
    BudgetGate,
    CurrencyPolicy,
)
from chief.core.session import Delta, Final, Milestone, ToolEnd, ToolStart, TurnEvent
from chief.core.tasks import (
    CLOSE_CONFIRM,
    NOTHING_TO_CLOSE,
    RENAME_CONFIRM,
    SessionProto,
    TaskManager,
)
from chief.gate.approvals import ApprovalAction, ApprovalManager, ApprovalRegistry
from chief.gate.blacklist import Blacklist
from chief.gate.policy import PolicyStore
from chief.gate.types import ToolPermissionContext
from chief.obs.audit import AuditLog
from chief.persistence import usage
from chief.persistence.messages import ROLE_CHIEF, MessageLog
from chief.persistence.models import MessageLogEntry
from chief.persistence.schedules import ACTION_MESSAGE, KIND_ONCE, create_schedule
from chief.persistence.tasks import DONE, get_or_create_task, get_task
from test_broadcast_bus import _RecordingInner
from test_tasks import FakeSession, wait_for_task_open

Factory = Callable[..., SessionProto]


class CliStack(NamedTuple):
    """What the ``cli_stack`` fixture builds: the running server + its outbound IO."""

    server: SocketServer
    io: CliTaskIO


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
    message_limit: int = CLI_LIMIT,
    skills_enabled: bool = False,
    skills_plugin_path: str | None = None,
    default_skills: tuple[str, ...] = (),
    chief_skills_dir: str | None = None,
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
        message_limit=message_limit,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        skills_enabled=skills_enabled,
        skills_plugin_path=skills_plugin_path,
        default_skills=default_skills,
        chief_skills_dir=chief_skills_dir,
    )


def _mirror_manager(
    session_factory: async_sessionmaker[AsyncSession],
    server: SocketServer,
    inner: Any,
    *,
    factory: Factory,
) -> tuple[TaskManager, Any]:
    """A real ``platform="telegram"`` engine mirrored onto the socket (#134 fixture).

    Built the way ``app.build_stacks`` builds a chat stack: ``inner`` (the platform bot
    seam) wrapped in a real :class:`MirrorTaskIO` over the shared real
    :class:`MessageLog`, so a foreign-platform thread's turn lands real rows in
    ``message_log`` for ``switch``'s backfill to read.
    """
    io = MirrorTaskIO(
        inner, platform="telegram", log=MessageLog(session_factory), server=server
    )
    manager = TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        platform="telegram",
        concurrency=3,
        turn_timeout=1000.0,
        idle_archive_seconds=1000.0,
        compaction_idle_seconds=1000.0,
        message_limit=CLI_LIMIT,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
    )
    return manager, inner


async def _running_server(path: str) -> tuple[SocketServer, asyncio.Task[None]]:
    server = SocketServer(path)
    task = asyncio.create_task(server.run())
    await asyncio.wait_for(server.started.wait(), 5)
    return server, task


@pytest.fixture
async def cli_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Callable[..., Awaitable[CliStack]]]:
    """Bring up a real server + CLI stack; the test supplies the fake LLM sessions.

    The stack shares a real :class:`MessageLog` over the ``session_factory`` fixture, so
    tests can inspect logged rows / drive detach-replay by querying that same DB.
    """
    built: list[tuple[SocketServer, asyncio.Task[None], TaskManager]] = []

    async def _build(
        sessions: Sequence[FakeSession],
        *,
        message_limit: int = CLI_LIMIT,
        replay_limit: int = 100,
        backfill_limit: int = 50,
        log: MessageLog | None = None,
        foreign: dict[str, ForeignPlatform] | None = None,
        budget: BudgetGate | None = None,
        skills_enabled: bool = False,
        skills_plugin_path: str | None = None,
        default_skills: tuple[str, ...] = (),
        chief_skills_dir: str | None = None,
    ) -> CliStack:
        server, task = await _running_server(str(tmp_path / "cli.sock"))
        # One log for BOTH sides, as production wires it — a test may pass its own
        # (e.g. _GatedLog) to drive the emit/attach interleave.
        log = log or MessageLog(session_factory)
        io = CliTaskIO(server, log=log)
        manager = _cli_manager(
            session_factory,
            io,
            factory=_seq_factory(sessions),
            message_limit=message_limit,
            skills_enabled=skills_enabled,
            skills_plugin_path=skills_plugin_path,
            default_skills=default_skills,
            chief_skills_dir=chief_skills_dir,
        )
        CliAdapter(
            server=server, engine=manager, log=log, replay_limit=replay_limit,
            session_factory=session_factory, backfill_limit=backfill_limit,
            foreign=foreign,
            budget=budget,
        )
        built.append((server, task, manager))
        return CliStack(server, io)

    try:
        yield _build
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
    server, _io = await cli_stack([session])
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


async def test_user_frame_streams_live_events_and_backfill_keeps_milestone(
    cli_stack: Callable[..., Any],
) -> None:
    # Live streaming on the CLI stack: the socket carries structured tool/delta
    # frames in stream order, and a switch backfill afterwards still shows the
    # tool start as its plain "using Bash" milestone row — deltas and tool ends
    # left no rows behind.
    events: list[TurnEvent] = [
        ToolStart(tool_call_id="c1", name="Bash"),
        Delta(message_id="m1", text="hel"),
        ToolEnd(tool_call_id="c1", ok=True),
        Delta(message_id="m1", text="", done=True),
    ]
    session = FakeSession(model="m", milestones=events)
    server, _io = await cli_stack([session])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "hello")).encode() + b"\n")
        await writer.drain()

        start = await _read_frame(reader)
        assert start["type"] == "tool"
        assert (start["tool_call_id"], start["name"], start["status"]) == (
            "c1",
            "Bash",
            "start",
        )
        delta = await _read_frame(reader)
        assert (delta["type"], delta["text"], delta["done"]) == ("delta", "hel", False)
        end = await _read_frame(reader)
        assert (end["type"], end["status"], end["ok"]) == ("tool", "end", True)
        done = await _read_frame(reader)
        assert (done["type"], done["done"]) == ("delta", True)
        reply = await _read_frame(reader)
        assert (reply["type"], reply["text"]) == ("reply", "reply:hello")

        writer.write(json.dumps(switch_frame("cli", "cli:main")).encode() + b"\n")
        await writer.drain()
        backfill = await _read_frame(reader)
        assert backfill["type"] == "backfill"
        rows = [
            (m["kind"], m["text"])
            for m in backfill["messages"]  # type: ignore[attr-defined]
            if m["role"] == "chief"
        ]
        assert rows == [("milestone", "using Bash"), ("reply", "reply:hello")]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_user_frame_missing_text_gets_invalid_fields(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack([])
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
    server, _io = await cli_stack([])
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
    server, _io = await cli_stack([])
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


async def test_close_command_marks_the_thread_done(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Session management over the wire: a real turn opens the task, then a
    # forwarded /close finishes it — status flips DONE, so it drops out of /tasks.
    session = FakeSession(model="m")
    server, _io = await cli_stack([session])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(user_frame("cli:main", "start")).encode() + b"\n")
        await writer.drain()
        assert (await _read_frame(reader))["text"] == "reply:start"
        await wait_for_task_open(
            session_factory, platform="cli", thread_key="cli:main"
        )

        writer.write(json.dumps(command_frame("cli:main", "close")).encode() + b"\n")
        await writer.drain()
        closed = await _read_frame(reader)
        assert closed["type"] == "reply"
        assert closed["text"] == CLOSE_CONFIRM

        async with session_factory() as db_session:
            task = await get_task(
                db_session, platform="cli", thread_key="cli:main"
            )
        assert task is not None and task.status == DONE
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_close_with_no_task_replies_nothing_to_close(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(command_frame("cli:main", "close")).encode() + b"\n")
        await writer.drain()
        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["text"] == NOTHING_TO_CLOSE
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_rename_command_retitles_the_thread_in_listings(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    session = FakeSession(model="m")
    server, _io = await cli_stack([session])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(user_frame("cli:main", "start")).encode() + b"\n")
        await writer.drain()
        assert (await _read_frame(reader))["text"] == "reply:start"
        await wait_for_task_open(
            session_factory, platform="cli", thread_key="cli:main"
        )

        writer.write(
            json.dumps(
                command_frame("cli:main", "rename", "Trip planning")
            ).encode()
            + b"\n"
        )
        await writer.drain()
        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["text"] == RENAME_CONFIRM.format(title="Trip planning")

        # The new title is what /tasks now lists — management is visible.
        writer.write(json.dumps(list_threads_frame()).encode() + b"\n")
        await writer.drain()
        threads = await _read_frame(reader)
        assert threads["type"] == "threads"
        titles = [t["title"] for t in cast(list[dict[str, Any]], threads["threads"])]
        assert "Trip planning" in titles
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_rename_without_argument_prints_usage(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        await _read_frame(reader)  # hello
        writer.write(json.dumps(command_frame("cli:main", "rename")).encode() + b"\n")
        await writer.drain()
        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["text"] == "Usage: /rename <title>"
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
    server, _io = await cli_stack([session_a, session_b])
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


async def test_oversized_reply_streams_a_real_file_frame(
    cli_stack: Callable[..., Any],
) -> None:
    # The M8 file path, driven end-to-end (no monkeypatched broadcast): a reply over a
    # small injected message_limit crosses should_send_as_file, so the engine emits
    # send_file → a REAL file frame over the REAL socket, encode() and all. The client
    # base64-decodes the data back to the exact reply bytes.
    session = FakeSession(model="m")
    server, _io = await cli_stack([session], message_limit=32)  # threshold 32×4=128
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        long_text = "x" * 200  # reply "reply:xxx…" is 206 chars > 128 → file frame
        writer.write(json.dumps(user_frame("cli:main", long_text)).encode() + b"\n")
        await writer.drain()
        frame = await _read_frame(reader)
        assert frame["type"] == "file"
        assert frame["platform"] == "cli"
        assert frame["thread_key"] == "cli:main"
        assert base64.b64decode(str(frame["data"])) == f"reply:{long_text}".encode()
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


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


async def _all_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[MessageLogEntry]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(MessageLogEntry).order_by(MessageLogEntry.id)
                )
            )
            .scalars()
            .all()
        )


async def _wait_for_rows(
    session_factory: async_sessionmaker[AsyncSession],
    predicate: Callable[[MessageLogEntry], bool],
) -> list[MessageLogEntry]:
    """Poll the log until at least one row satisfies ``predicate`` (bounded 5s)."""

    async def _poll() -> list[MessageLogEntry]:
        while True:
            matched = [r for r in await _all_rows(session_factory) if predicate(r)]
            if matched:
                return matched
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), 5)


async def test_emit_records_undelivered_when_no_clients(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The outbound recording seam in isolation: with no client attached, a reply is
    # broadcast to nobody AND logged held (delivered=False) with its wire frame payload.
    log = MessageLog(session_factory)
    io = CliTaskIO(SocketServer("/unused"), log=log)
    await io.send("cli:main", "hi")

    (row,) = await _all_rows(session_factory)
    assert row.kind == "reply" and row.role == "chief"
    assert row.delivered is False
    assert row.payload is not None
    assert json.loads(row.payload) == {
        "type": "reply", "platform": "cli", "thread_key": "cli:main", "text": "hi"
    }


async def test_detach_replay_delivers_missed_reply_then_live_traffic(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The issue's central mechanism, end-to-end over a real socket: a turn's reply lands
    # while the client is detached → it is held → on reattach it replays marked, then a
    # fresh live turn's reply follows unmarked. Only the LLM is faked.
    gate = asyncio.Event()
    started = asyncio.Event()
    session = FakeSession(model="m", gate=gate, on_start=started.set)
    server, _io = await cli_stack([session])

    reader, writer = await asyncio.open_unix_connection(server.path)
    assert (await _read_frame(reader))["type"] == "hello"
    writer.write(json.dumps(user_frame("cli:main", "hello")).encode() + b"\n")
    await writer.drain()
    await asyncio.wait_for(started.wait(), 5)  # turn is parked mid-flight

    # Detach and wait for the server to reap the connection, so the reply that follows
    # snapshots delivered=False (there is genuinely no client attached at emit time).
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()

    async def _reaped() -> None:
        while server.has_clients:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_reaped(), 5)

    gate.set()  # let the turn finish → the reply is emitted with no client attached
    await _wait_for_rows(
        session_factory,
        lambda r: r.kind == "reply" and r.delivered is False,
    )

    # Reattach: the held reply replays (marked) before any live traffic on the socket.
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        replayed = await _read_frame(reader)
        assert replayed["type"] == "reply"
        assert replayed["thread_key"] == "cli:main"
        assert replayed["text"] == "reply:hello"
        assert replayed["replay"] is True

        # A fresh live turn on the same connection: its reply arrives UNmarked.
        writer.write(json.dumps(user_frame("cli:main", "again")).encode() + b"\n")
        await writer.drain()
        live = await _read_frame(reader)
        assert live["type"] == "reply"
        assert live["text"] == "reply:again"
        assert "replay" not in live
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


class _GatedLog(MessageLog):
    """A :class:`MessageLog` whose ``record`` parks until a gate opens (#134 harness).

    Holds an emit *inside* its unrecorded window — broadcast already done, row not yet
    committed — so a test can attach a client at exactly that instant. That interleave
    is the emit/attach race itself, not a simulation of it: everything else (server,
    adapter, claim, DB) is real.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        gate: asyncio.Event,
    ) -> None:
        super().__init__(session_factory)
        self._gate = gate
        #: Set once an emit is parked mid-record — the cue that the window is open.
        self.entered = asyncio.Event()

    async def record(self, **kwargs: Any) -> None:
        self.entered.set()
        await self._gate.wait()
        await super().record(**kwargs)


async def test_attach_racing_an_unrecorded_emit_replays_exactly_once(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # D1, the emit/attach ordering hole: an emit snapshots has_clients=False, broadcasts
    # to nobody, then yields inside record(). A client attaching in THAT window used to
    # claim-replay (finding nothing, because the row is not committed yet) and then join
    # the broadcast set — so the frame reached neither this client (never broadcast to)
    # nor a later one without being a re-delivery. The delivery barrier makes the two
    # orders mutually exclusive: the claim cannot run until the row is committed.
    gate = asyncio.Event()
    log = _GatedLog(session_factory, gate=gate)
    server, io = await cli_stack([], log=log)

    emit = asyncio.create_task(io.send("cli:main", "held"))
    await asyncio.wait_for(log.entered.wait(), 5)  # broadcast done, row NOT committed

    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        # The connect hook's claim is now blocked on the barrier. Let the emit finish:
        # it commits the held row, THEN releases — so the claim must see it.
        gate.set()
        await emit

        replayed = await _read_frame(reader)
        assert replayed["text"] == "held"
        assert replayed["replay"] is True  # held, then replayed — not delivered live

        # Exactly once: the claim marked it delivered, so nothing is queued again.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()

    # And a second attach re-delivers nothing — the durable proof it was claimed once.
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_second_reattach_does_not_redeliver(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The claim marks every undelivered row delivered, so once a held reply has replayed
    # onto one reattach, a second reattach gets nothing queued ahead of live traffic.
    gate = asyncio.Event()
    started = asyncio.Event()
    session = FakeSession(model="m", gate=gate, on_start=started.set)
    server, _io = await cli_stack([session])

    reader, writer = await asyncio.open_unix_connection(server.path)
    assert (await _read_frame(reader))["type"] == "hello"
    writer.write(json.dumps(user_frame("cli:main", "hello")).encode() + b"\n")
    await writer.drain()
    await asyncio.wait_for(started.wait(), 5)
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()

    async def _reaped() -> None:
        while server.has_clients:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_reaped(), 5)
    gate.set()
    await _wait_for_rows(
        session_factory, lambda r: r.kind == "reply" and r.delivered is False
    )

    # First reattach consumes the replay.
    reader, writer = await asyncio.open_unix_connection(server.path)
    assert (await _read_frame(reader))["type"] == "hello"
    assert (await _read_frame(reader))["text"] == "reply:hello"
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()

    # D2 guard: the claim commits delivered=True BEFORE it sends, so having read the
    # replay above proves the update is committed — it must also be DURABLE. A session
    # that shared this one's transaction could roll it back, reviving the row as
    # undelivered; that is precisely what re-delivered the reply on the next attach.
    assert all(row.delivered for row in await _all_rows(session_factory))

    # Second reattach: nothing held → ping is answered directly, no replay frame first.
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_wedged_client_replay_does_not_freeze_emit_or_attach(
    cli_stack: Callable[..., Any],
) -> None:
    # The barrier must never be held across client backpressure. The connect hook's
    # claim-replay runs INSIDE the delivery barrier; if it drained there, a client that
    # stopped reading (a Ctrl-Z'd terminal) would park that drain with the barrier held
    # and freeze EVERY emit, mirror and attach in the daemon until it died. A wedged
    # client must stall only its own connection.
    server, io = await cli_stack([])

    # Held while detached, and far past what the socket can absorb without a reader:
    # ~4 MB of base64 vs a 64 KB transport high-water mark and ~0.5 MB of kernel +
    # client buffers. The replay write therefore CANNOT flush — a drain on it parks.
    await io.send_file("cli:main", "big.bin", b"x" * 3_000_000)

    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        # Read one byte of the replay and then stop reading: the byte proves the hook is
        # mid-send inside the barrier (nothing else follows the hello), and stopping is
        # what wedges the connection. No sleep, no poll — the byte IS the sync point.
        assert await asyncio.wait_for(reader.read(1), 5)

        # The daemon must stay live. Pre-fix both of these hang on the wedged client's
        # drain: the emit blocks on the barrier, and so does the new connection's hook.
        await asyncio.wait_for(io.send("cli:main", "live"), 5)

        other_reader, other_writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(other_reader))["type"] == "hello"
            other_writer.write(b'{"type":"ping"}\n')
            await other_writer.drain()
            assert await _read_frame(other_reader) == {"type": "pong"}
        finally:
            other_writer.close()
            with suppress(OSError):
                await other_writer.wait_closed()
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_log_records_both_directions_with_role_surface_timestamps(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One attached turn end-to-end logs exactly one inbound owner row and one outbound
    # chief row, each with role/surface/kind and a timestamp.
    session = FakeSession(model="m")
    server, _io = await cli_stack([session])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "hello")).encode() + b"\n")
        await writer.drain()
        reply = await _read_frame(reader)
        assert reply["text"] == "reply:hello"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()

    rows = await _wait_for_rows(session_factory, lambda r: r.kind == "reply")
    all_rows = await _all_rows(session_factory)
    assert len(all_rows) == 2
    inbound = next(r for r in all_rows if r.role == "owner")
    outbound = rows[0]
    assert inbound.kind == "user" and inbound.surface == "dm"
    assert inbound.delivered is True and inbound.payload is None
    assert inbound.created_at is not None
    assert outbound.role == "chief" and outbound.surface == "dm"
    assert outbound.delivered is True and outbound.payload is not None
    assert outbound.created_at is not None


async def test_replay_window_bounds_reattach(
    cli_stack: Callable[..., Any],
) -> None:
    # replay_limit caps the replayed window to the most-recent N held frames; the claim
    # still marks the rest delivered, so nothing else queues ahead of live traffic.
    server, io = await cli_stack([], replay_limit=2)
    for i in range(3):
        await io.send("cli:main", f"m{i}")  # no client attached → all held

    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        first = await _read_frame(reader)
        second = await _read_frame(reader)
        assert [first["text"], second["text"]] == ["m1", "m2"]  # most-recent two
        assert first["replay"] is True and second["replay"] is True
        # Nothing else queued: ping is answered directly.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await _read_frame(reader) == {"type": "pong"}
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


# ---- #139: a real Scheduler tick over the production build_cli_stack wiring ----


class SchedulerStack(NamedTuple):
    """What the ``scheduler_stack`` fixture builds: the production wiring, unstarted."""

    server: SocketServer
    stack: Any  # app.Stack: (TaskManager, CliAdapter, ApprovalManager)
    scheduler: Any  # chief.core.scheduler.Scheduler
    start: Callable[[], "asyncio.Task[None]"]


@pytest.fixture
async def scheduler_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[SchedulerStack]:
    """The real ``build_cli_stack`` + ``build_scheduler`` wiring, tokenless, on ``cli``.

    Mirrors ``cli_stack``'s builder + cleanup shape, but wires the production app
    builders (#139's central mechanism) instead of hand-assembling a ``TaskManager``.
    """
    settings = Settings(
        owner_telegram_id=None,
        telegram_bot_token=None,
        owner_discord_id=None,
        discord_bot_token=None,
        scheduler_enabled=True,
        primary_platform="cli",
        primary_thread_key="cli:main",
        scheduler_tick_seconds=0.01,
        memory_git=False,
        memory_dir=str(tmp_path / "memory"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
    )
    server, server_task = await _running_server(str(tmp_path / "sched.sock"))
    audit = AuditLog(settings.audit_log_path)
    policy = PolicyStore(session_factory, audit=audit)
    memory = app.build_memory(settings)
    stack = app.build_cli_stack(
        settings,
        socket_server=server,
        session_factory=session_factory,
        policy=policy,
        audit=audit,
        memory=memory,
    )
    scheduler, http = app.build_scheduler(
        settings, stacks=[stack], session_factory=session_factory
    )
    assert scheduler is not None and http is None

    tick_tasks: list[asyncio.Task[None]] = []

    def _start() -> asyncio.Task[None]:
        task = asyncio.create_task(scheduler.run())
        tick_tasks.append(task)
        return task

    try:
        yield SchedulerStack(server, stack, scheduler, _start)
    finally:
        for task in tick_tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await stack[0].shutdown()  # the TaskManager — kills background consumers first
        await server.stop()
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task


async def _seed_due_reminder(
    session_factory: async_sessionmaker[AsyncSession], *, text: str = "stand up"
) -> None:
    async with session_factory() as s:
        await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action=text,
            action_type=ACTION_MESSAGE,
            next_run=datetime.now(UTC) - timedelta(seconds=1),  # already due
            thread_key="cli:main",
        )


async def test_scheduler_tick_delivers_reminder_to_connected_client(
    scheduler_stack: SchedulerStack,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The issue's central mechanism, end-to-end: a real Schedule row, fired by a real
    # Scheduler.run() tick over the production build_cli_stack wiring, arrives at a
    # connected socket client as a tagged reply frame.
    reader, writer = await asyncio.open_unix_connection(scheduler_stack.server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"

        await _seed_due_reminder(session_factory)
        scheduler_stack.start()

        frame = await _read_frame(reader)
        assert frame == {
            "type": "reply",
            "platform": "cli",
            "thread_key": "cli:main",
            "text": "stand up",
        }
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_scheduler_reminder_fired_while_detached_replays_on_attach(
    scheduler_stack: SchedulerStack,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Same wiring, no client attached when the reminder fires: it is held (the #133
    # mirror also writes a second, payload-less row for the same send — filter on
    # payload is not None so we wait for the real frame row, not the mirror's), then
    # replays marked on the next attach.
    await _seed_due_reminder(session_factory)
    scheduler_stack.start()

    await _wait_for_rows(
        session_factory,
        lambda r: r.kind == "reply" and r.payload is not None and r.delivered is False,
    )

    reader, writer = await asyncio.open_unix_connection(scheduler_stack.server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        frame = await _read_frame(reader)
        assert frame["type"] == "reply"
        assert frame["platform"] == "cli"
        assert frame["thread_key"] == "cli:main"
        assert frame["text"] == "stand up"
        assert frame["replay"] is True
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


# ---- #136: multi-surface approvals — cards + answers over the socket ----------


class _GateSession:
    """A minimal SessionProto that drives the REAL ``can_use_tool`` mid-turn (#136).

    Unlike ``FakeSession`` (a fixed milestones→Final sequence), this session actually
    calls the captured gate callback with a blacklisted shell command, so the central
    mechanism — classify → ASK → a real approval card → a real socket answer — runs for
    real rather than being mocked out.
    """

    def __init__(self, *, model: str, resume: str | None, can_use_tool: Any) -> None:
        self.model = model
        self.resume = resume
        self.session_id = resume
        self.last_cost_usd = 0.0
        self.last_rate_limit_status: str | None = None
        self.last_served_model: str | None = None
        self.last_premium_requests: dict[str, int] = {}
        self._can_use_tool = can_use_tool

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        result = await self._can_use_tool(
            "mcp__chief_shell__bash",
            {"command": "sudo rm -rf /"},
            ToolPermissionContext(),
        )
        self.session_id = f"sess-{text}"
        self.last_served_model = self.model
        yield Final(text="proceeded" if result.behavior == "allow" else "blocked")

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str) -> None:
        self.model = model

    async def aclose(self) -> None: ...

    async def force_close(self) -> None: ...


def _gate_factory() -> Factory:
    def factory(
        *, model: str, resume: str | None = None, can_use_tool: Any = None, **_: Any
    ) -> SessionProto:
        return _GateSession(model=model, resume=resume, can_use_tool=can_use_tool)

    return factory


class GateStack(NamedTuple):
    """A real CLI stack wired for the gate: policy + approvals + audit + blacklist."""

    server: SocketServer
    registry: ApprovalRegistry


@pytest.fixture
async def gate_cli_stack(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[GateStack]:
    """A real SocketServer + CliAdapter + TaskManager wired with a real gate (#136).

    Unlike ``cli_stack``, this manager carries ``policy``/``approvals``/``audit``/
    ``blacklist`` so ``TaskManager`` actually wires a ``can_use_tool`` into each
    session (``tasks.py:1140``) — the central mechanism this slice adds.
    """
    server, task = await _running_server(str(tmp_path / "gate.sock"))
    log = MessageLog(session_factory)
    registry = ApprovalRegistry()
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    policy = PolicyStore(session_factory, audit=audit)
    await policy.load()
    io = CliTaskIO(server, log=log)
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        registry=registry,
        timeout_seconds=5.0,
    )
    manager = TaskManager(
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
        session_factory_sdk=_gate_factory(),
        stop_intent=_no,
        warrants_task=_no,
        policy=policy,
        approvals=approvals,
        audit=audit,
        blacklist=Blacklist.from_config(),
    )
    CliAdapter(server=server, engine=manager, log=log, approvals=registry)
    try:
        yield GateStack(server, registry)
    finally:
        await manager.shutdown()
        await server.stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_answer_approve_once_lets_the_blacklisted_command_proceed(
    gate_cli_stack: GateStack,
) -> None:
    # AC1 (approve): a blacklisted command raises a real card frame carrying its
    # approval_id, preview text, and the four options; approve_once lets it proceed.
    reader, writer = await asyncio.open_unix_connection(gate_cli_stack.server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "go")).encode() + b"\n")
        await writer.drain()

        card = await _read_frame(reader)
        assert card["type"] == "card"
        assert card["platform"] == "cli"
        assert card["text"] == "Run: sudo rm -rf /"
        assert len(card["options"]) == 4  # type: ignore[arg-type]
        approval_id = cast(int, card["approval_id"])

        writer.write(
            json.dumps(answer_frame(approval_id, "approve_once")).encode() + b"\n"
        )
        await writer.drain()

        resolved = await _read_frame(reader)
        assert resolved["type"] == "card_resolved"
        assert resolved["approval_id"] == approval_id

        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["text"] == "proceeded"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_answer_deny_once_blocks_the_command(
    gate_cli_stack: GateStack,
) -> None:
    # AC1 (deny): same card, but deny_once blocks the command.
    reader, writer = await asyncio.open_unix_connection(gate_cli_stack.server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "go")).encode() + b"\n")
        await writer.drain()

        card = await _read_frame(reader)
        approval_id = cast(int, card["approval_id"])

        writer.write(
            json.dumps(answer_frame(approval_id, "deny_once")).encode() + b"\n"
        )
        await writer.drain()

        resolved = await _read_frame(reader)
        assert resolved["type"] == "card_resolved"

        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["text"] == "blocked"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_second_answer_for_a_resolved_approval_is_already_resolved(
    gate_cli_stack: GateStack,
) -> None:
    # AC3: resolve.resolve returns False for a duplicate/unknown id — the socket
    # surfaces that as an already_resolved error, never a second decide/edit.
    reader, writer = await asyncio.open_unix_connection(gate_cli_stack.server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "go")).encode() + b"\n")
        await writer.drain()

        card = await _read_frame(reader)
        approval_id = cast(int, card["approval_id"])
        writer.write(
            json.dumps(answer_frame(approval_id, "approve_once")).encode() + b"\n"
        )
        await writer.drain()
        await _read_frame(reader)  # card_resolved
        await _read_frame(reader)  # reply

        writer.write(
            json.dumps(answer_frame(approval_id, "deny_once")).encode() + b"\n"
        )
        await writer.drain()
        error = await _read_frame(reader)
        assert error == {
            "type": "error",
            "code": "already_resolved",
            "message": f"approval {approval_id} is unknown or already resolved",
        }
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_cross_surface_card_and_answer_resolve_a_foreign_platform_approval(
    gate_cli_stack: GateStack,
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # AC2: a card raised on a *telegram* thread (real ApprovalManager over a real
    # MirrorTaskIO wrapping a fake platform IO) appears on BOTH the platform IO seam
    # and — platform-tagged — the socket. Both directions of resolve are exercised: a
    # socket answer resolving a chat-raised card, and a chat-button-style resolve
    # (registry.resolve directly) reaching the socket client as card_resolved.
    server, registry = gate_cli_stack
    fake_platform_io = _RecordingInner()
    telegram_audit = AuditLog(str(tmp_path / "telegram-audit.jsonl"))
    telegram_policy = PolicyStore(session_factory, audit=telegram_audit)
    await telegram_policy.load()
    mirror = MirrorTaskIO(
        fake_platform_io,
        platform="telegram",
        log=MessageLog(session_factory),
        server=server,
    )
    telegram_manager = ApprovalManager(
        session_factory=session_factory,
        io=mirror,
        policy=telegram_policy,
        audit=telegram_audit,
        registry=registry,
        timeout_seconds=5.0,
    )

    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"

        parked = asyncio.create_task(
            telegram_manager.request(
                task_id=None,
                thread_key="telegram:room1",
                tier="owner",
                tool_name="Bash",
                tool_input={"command": "git push"},
                route="telegram:room1",
            )
        )
        card = await _read_frame(reader)
        assert card["type"] == "card"
        assert card["platform"] == "telegram"
        approval_id = cast(int, card["approval_id"])
        # The platform IO seam recorded the card too — both surfaces got it.
        assert fake_platform_io.cards
        assert fake_platform_io.cards[0][0] == "telegram:room1"

        # (a) forward: a socket answer resolves a card raised by a chat stack.
        writer.write(
            json.dumps(answer_frame(approval_id, "approve_once")).encode() + b"\n"
        )
        await writer.drain()
        allowed = await parked

        assert allowed is True
        resolved = await _read_frame(reader)
        assert resolved["type"] == "card_resolved"
        assert resolved["platform"] == "telegram"
        assert resolved["approval_id"] == approval_id
        assert "by cli" in fake_platform_io.edits[-1][1]

        # (b) reverse: a chat-button-style resolve (registry.resolve directly) reaches
        # the socket client as a card_resolved frame naming the decider.
        parked2 = asyncio.create_task(
            telegram_manager.request(
                task_id=None,
                thread_key="telegram:room1",
                tier="owner",
                tool_name="Bash",
                tool_input={"command": "git status"},
                route="telegram:room1",
            )
        )
        card2 = await _read_frame(reader)
        approval_id2 = cast(int, card2["approval_id"])

        won = await registry.resolve(
            approval_id2, ApprovalAction.APPROVE_ONCE, decided_by="42"
        )
        assert won is True
        assert await parked2 is True
        resolved2 = await _read_frame(reader)
        assert resolved2["type"] == "card_resolved"
        assert "by 42" in str(resolved2["text"])

        # A subsequent socket answer for that same id is refused already_resolved.
        writer.write(
            json.dumps(answer_frame(approval_id2, "deny_once")).encode() + b"\n"
        )
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "already_resolved"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


# ---- #134: cross-platform navigation — list_threads + stateless switch backfill ----


async def test_list_threads_returns_every_platforms_threads(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The central mechanism: a real CLI turn and a real (mirrored) telegram turn both
    # leave a task row, and list_threads reads the tasks table across BOTH platforms.
    server, _io = await cli_stack([FakeSession(model="m")])
    tg_manager, _inner = _mirror_manager(
        session_factory,
        server,
        _RecordingInner(),
        factory=_seq_factory([FakeSession(model="m")]),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            writer.write(json.dumps(user_frame("cli:main", "hi")).encode() + b"\n")
            await writer.drain()
            assert (await _read_frame(reader))["text"] == "reply:hi"
            # The task's status flip to OPEN lands after the reply — engine bookkeeping,
            # not the delivery race, so sync on it before reading the tasks table.
            await wait_for_task_open(
                session_factory, platform="cli", thread_key="cli:main"
            )

            await tg_manager.dispatch(
                thread_key="-100:5", text="hi", surface=Surface.DM
            )
            mirrored = await _read_frame(reader)  # drain the mirrored reply
            assert mirrored["platform"] == "telegram"
            await wait_for_task_open(
                session_factory, platform="telegram", thread_key="-100:5"
            )

            writer.write(json.dumps(list_threads_frame()).encode() + b"\n")
            await writer.drain()
            frame = await _read_frame(reader)
            assert frame["type"] == "threads"
            threads = {
                (t["platform"], t["thread_key"]): t
                for t in frame["threads"]  # type: ignore[attr-defined]
            }
            assert set(threads) == {("cli", "cli:main"), ("telegram", "-100:5")}
            for entry in threads.values():
                assert entry["status"] == "open"
                assert "title" in entry
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_switch_backfills_foreign_thread_then_live_frames_follow(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The stateless handoff: switch answers ONE backfill batch of the mirrored chief
    # lines in order, and a subsequent live turn on the same thread still arrives by
    # unmarked broadcast — no server-side subscription was ever created.
    server, _io = await cli_stack([])
    tg_manager, _inner = _mirror_manager(
        session_factory,
        server,
        _RecordingInner(),
        factory=_seq_factory(
            [
                FakeSession(model="m", milestones=[Milestone(text="using Bash")]),
                FakeSession(model="m"),
            ]
        ),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            await tg_manager.dispatch(
                thread_key="-100:5", text="hi", surface=Surface.DM
            )
            milestone = await _read_frame(reader)
            assert milestone["type"] == "milestone"
            reply = await _read_frame(reader)
            assert reply["type"] == "reply"

            # NO wait for the rows here — not because reading a frame proves its row is
            # committed (it does not: the mirror broadcasts, THEN records, both inside
            # the barrier), but because the reader is itself a barrier party. The switch
            # takes the barrier to read the log, so it queues behind any mirror record
            # still in flight. If this is ever short, the barrier is broken; do not
            # paper it over.
            writer.write(
                json.dumps(switch_frame("telegram", "-100:5")).encode() + b"\n"
            )
            await writer.drain()
            backfill = await _read_frame(reader)
            assert backfill["type"] == "backfill"
            assert backfill["platform"] == "telegram"
            assert backfill["thread_key"] == "-100:5"
            texts = [m["text"] for m in backfill["messages"]]  # type: ignore[attr-defined]
            assert texts == ["using Bash", "reply:hi"]

            await tg_manager.dispatch(
                thread_key="-100:5", text="again", surface=Surface.DM
            )
            # Same underlying session (thread already active): it streams its fixed
            # milestone again before the reply — drain that, then check the reply.
            live_milestone = await _read_frame(reader)
            assert live_milestone["type"] == "milestone"
            live = await _read_frame(reader)
            assert live["type"] == "reply"
            assert live["platform"] == "telegram"
            assert "backfill" not in live and "replay" not in live
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_backfill_is_bounded_and_distinct_from_live(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Bounded: backfill_limit=3 returns only the most-recent three rows, in order, and
    # its frame type is "backfill" (not reply/milestone) — the distinctness marker.
    server, _io = await cli_stack([FakeSession(model="m")], backfill_limit=3)
    log = MessageLog(session_factory)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        # A real turn first, so a task row exists for cli:main.
        writer.write(json.dumps(user_frame("cli:main", "hi")).encode() + b"\n")
        await writer.drain()
        assert (await _read_frame(reader))["text"] == "reply:hi"

        # NO wait for the reply's row — take the barrier instead. Reading the frame does
        # NOT prove its row is committed: CliTaskIO broadcasts the bytes and only then
        # awaits record(), both inside the barrier, so the client can see the reply
        # while the row is still in flight. The barrier orders its *parties*, and these
        # seeding records are not one — so become one. Acquiring it here can only
        # succeed once the emit has released it, i.e. once record() has committed, which
        # is what puts the reply's id below the six rows the bound is asserted against.
        async with server.delivery_lock:
            pass

        for i in range(6):
            await log.record(
                platform="cli", thread_key="cli:main", role=ROLE_CHIEF,
                kind="reply", text=f"m{i}", payload=None,
            )

        writer.write(json.dumps(switch_frame("cli", "cli:main")).encode() + b"\n")
        await writer.drain()
        backfill = await _read_frame(reader)
        assert backfill["type"] == "backfill"
        texts = [m["text"] for m in backfill["messages"]]  # type: ignore[attr-defined]
        assert texts == ["m3", "m4", "m5"]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_switch_to_unknown_thread_errors(cli_stack: Callable[..., Any]) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(switch_frame("telegram", "nope")).encode() + b"\n")
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_thread"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_switch_missing_fields_gets_invalid_fields(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(b'{"type":"switch","platform":"telegram"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "invalid_fields"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


# Cross-stack drive (#135): _handle_inject calls the exact same TaskManager.dispatch
# that every native turn uses (see _mirror_manager) — so the central mechanism is a
# real Engine.dispatch, not a wrapper, and steering/cancel/budget gating (AC #3) are
# inherited structurally from the existing dispatch test matrix (test_tasks.py)
# rather than re-tested here.


async def test_inject_dispatches_owner_turn_on_foreign_platform_with_echo(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([FakeSession(model="m")], foreign=foreign)
    inner = _RecordingInner()
    tg_manager, _inner = _mirror_manager(
        session_factory,
        server,
        inner,
        factory=_seq_factory([FakeSession(model="m"), FakeSession(model="m")]),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            # Seed the thread with one native turn so the task row exists.
            await tg_manager.dispatch(
                thread_key="-100:5", text="hi", surface=Surface.DM
            )
            seeded = await _read_frame(reader)  # the mirrored native reply
            assert seeded["platform"] == "telegram"
            await wait_for_task_open(
                session_factory, platform="telegram", thread_key="-100:5"
            )

            # Only now wire the foreign target — relies on the __init__ is-None
            # check (not `foreign or {}`) so the CliAdapter's dict reference stays
            # live even though it was empty at construction time.
            foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)

            writer.write(
                json.dumps(inject_frame("telegram", "-100:5", "inject me")).encode()
                + b"\n"
            )
            await writer.drain()
            reply = await _read_frame(reader)
            assert reply == {
                "type": "reply",
                "platform": "telegram",
                "thread_key": "-100:5",
                "text": "reply:inject me",
            }
            # inner.sends[-1] is the reply's own delivery through the mirror (the
            # normal outbound path); [-2] is the echo this handler wrote directly.
            assert inner.sends[-2] == ("-100:5", "📤 via CLI: inject me")
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_inject_to_unknown_thread_errors_with_no_dispatch(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([], foreign=foreign)
    inner = _RecordingInner()
    tg_manager, _inner = _mirror_manager(
        session_factory, server, inner, factory=_seq_factory([])
    )
    foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"
            writer.write(
                json.dumps(inject_frame("telegram", "nope", "hi")).encode() + b"\n"
            )
            await writer.drain()
            error = await _read_frame(reader)
            assert error["type"] == "error" and error["code"] == "unknown_thread"
            assert inner.sends == []
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_inject_unknown_platform_errors(cli_stack: Callable[..., Any]) -> None:
    server, _io = await cli_stack([], foreign={})
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(
            json.dumps(inject_frame("telegram", "anything", "hi")).encode() + b"\n"
        )
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "unknown_platform"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_inject_missing_fields_gets_invalid_fields(
    cli_stack: Callable[..., Any],
) -> None:
    foreign: dict[str, ForeignPlatform] = {"telegram": cast(Any, object())}
    server, _io = await cli_stack([], foreign=foreign)
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(b'{"type":"inject","platform":"telegram"}\n')
        await writer.drain()
        error = await _read_frame(reader)
        assert error["type"] == "error" and error["code"] == "invalid_fields"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_inject_records_owner_row_for_backfill(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([], foreign=foreign)
    inner = _RecordingInner()
    tg_manager, _inner = _mirror_manager(
        session_factory,
        server,
        inner,
        factory=_seq_factory([FakeSession(model="m"), FakeSession(model="m")]),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            await tg_manager.dispatch(
                thread_key="-100:5", text="hi", surface=Surface.DM
            )
            seeded = await _read_frame(reader)
            assert seeded["platform"] == "telegram"
            await wait_for_task_open(
                session_factory, platform="telegram", thread_key="-100:5"
            )
            foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)

            writer.write(
                json.dumps(inject_frame("telegram", "-100:5", "inject me")).encode()
                + b"\n"
            )
            await writer.drain()
            reply = await _read_frame(reader)
            assert reply["text"] == "reply:inject me"

            writer.write(
                json.dumps(switch_frame("telegram", "-100:5")).encode() + b"\n"
            )
            await writer.drain()
            backfill = await _read_frame(reader)
            assert backfill["type"] == "backfill"
            rows = {
                m["text"]: m for m in backfill["messages"]  # type: ignore[attr-defined]
            }
            assert rows["inject me"]["role"] == "owner"
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_inject_rejects_a_guest_tier_thread(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Review fix (#135): the sole (platform, thread_key) row can be a guest's — inject
    # must not trust it and hand the guest's thread the owner tool surface.
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([], foreign=foreign)
    inner = _RecordingInner()
    tg_manager, _inner = _mirror_manager(
        session_factory, server, inner, factory=_seq_factory([FakeSession(model="m")])
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            await tg_manager.dispatch_guest(
                thread_key="-100:9", text="hi", surface=Surface.DM
            )
            seeded = await _read_frame(reader)  # the mirrored guest receptionist reply
            assert seeded["platform"] == "telegram"
            await wait_for_task_open(
                session_factory, platform="telegram", thread_key="-100:9"
            )
            sends_before = len(inner.sends)
            foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)

            writer.write(
                json.dumps(inject_frame("telegram", "-100:9", "inject me")).encode()
                + b"\n"
            )
            await writer.drain()
            error = await _read_frame(reader)
            assert error["type"] == "error" and error["code"] == "unknown_thread"
            # No echo, no dispatch — the guest thread never sees another turn.
            assert len(inner.sends) == sends_before
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_inject_rejects_a_thread_with_no_proven_surface(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Review fix (#135): a task predating the ``surface`` column (or any row created
    # off a path that never recorded one) must fail closed, not be assumed DM.
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([], foreign=foreign)
    inner = _RecordingInner()
    tg_manager, _inner = _mirror_manager(
        session_factory, server, inner, factory=_seq_factory([])
    )
    async with session_factory() as session:
        await get_or_create_task(
            session, platform="telegram", thread_key="-100:legacy", tier="owner"
        )
    foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"
            writer.write(
                json.dumps(
                    inject_frame("telegram", "-100:legacy", "inject me")
                ).encode()
                + b"\n"
            )
            await writer.drain()
            error = await _read_frame(reader)
            assert error["type"] == "error" and error["code"] == "unknown_surface"
            assert inner.sends == []
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


async def test_inject_uses_the_threads_real_surface_for_a_rebuilt_group_thread(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Review fix (#135): dispatch/MessageLog.record used to hardcode surface=DM, so a
    # GROUP task rebuilt after eviction (restart/idle-archive — no live _RunningTask)
    # lost its group semantics (context drain, DM'd approval cards). The buffered
    # ambient line only reaches the turn if the real GROUP branch runs — proof the
    # thread's actual surface, not a hardcoded DM, drove this dispatch.
    foreign: dict[str, ForeignPlatform] = {}
    server, _io = await cli_stack([], foreign=foreign)
    inner = _RecordingInner()
    inject_session = FakeSession(model="m")
    tg_manager, _inner = _mirror_manager(
        session_factory,
        server,
        inner,
        factory=_seq_factory([FakeSession(model="m"), inject_session]),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            assert (await _read_frame(reader))["type"] == "hello"

            await tg_manager.dispatch(
                thread_key="-100:5:grp", text="hi", surface=Surface.GROUP
            )
            seeded = await _read_frame(reader)
            assert seeded["platform"] == "telegram"
            await wait_for_task_open(
                session_factory, platform="telegram", thread_key="-100:5:grp"
            )

            # Evict the running task — simulates the restart/idle-archive precondition
            # the review finding calls out, so the next turn has to rebuild it fresh.
            await tg_manager._stop_task(tg_manager._tasks["-100:5:grp"])
            await tg_manager.observe(
                thread_key="-100:5:grp", text="ambient hi", sender_name="bob"
            )

            foreign["telegram"] = ForeignPlatform(engine=tg_manager, io=inner)
            writer.write(
                json.dumps(
                    inject_frame("telegram", "-100:5:grp", "inject me")
                ).encode()
                + b"\n"
            )
            await writer.drain()
            reply = await _read_frame(reader)
            turn = inject_session.queries[-1]
            assert str(reply["text"]) == f"reply:{turn}"
            assert "bob: ambient hi" in turn
            assert turn.endswith("inject me")

            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(MessageLogEntry).where(
                                MessageLogEntry.text == "inject me"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert any(r.surface == "group" for r in rows)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    finally:
        await tg_manager.shutdown()


# ---- #138: /status + /skills ------------------------------------------------------


class _FakeBudgetIO:
    """Minimal ``BudgetIO`` stub — the ``/status`` test only needs ``record`` to work
    against a real ``BudgetGate``."""

    async def send(self, thread_key: str, text: str) -> None:
        pass

    async def send_budget_card(self, route: str, card: Any) -> None:
        pass


def _budget_gate(session_factory: async_sessionmaker[AsyncSession]) -> BudgetGate:
    return BudgetGate(
        session_factory=session_factory,
        io=_FakeBudgetIO(),
        owner_inbox="owner-inbox",
        policies={
            usage.PREMIUM_REQUESTS: CurrencyPolicy(
                cap=200.0,
                warn_fractions=(0.75, 0.90),
                exhaust_fraction=1.0,
                accumulation=ACCUM_MAX,
                action=ACTION_PAUSE,
            ),
        },
        owner_tz="UTC",
        anchor_day=1,
        now=lambda: datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )


async def test_status_returns_real_tasks_budget_and_schedules(
    cli_stack: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    budget = _budget_gate(session_factory)
    server, _io = await cli_stack([FakeSession(model="m")], budget=budget)
    await budget.record(usage.PREMIUM_REQUESTS, 50.0)
    async with session_factory() as session:
        await create_schedule(
            session,
            kind=KIND_ONCE,
            spec="2026-07-01T00:00:00+00:00",
            action="ping",
            action_type=ACTION_MESSAGE,
            next_run=datetime(2026, 7, 1, tzinfo=UTC),
            thread_key="cli:main",
        )
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(user_frame("cli:main", "hi")).encode() + b"\n")
        await writer.drain()
        assert (await _read_frame(reader))["text"] == "reply:hi"
        await wait_for_task_open(
            session_factory, platform="cli", thread_key="cli:main"
        )

        writer.write(json.dumps(status_frame()).encode() + b"\n")
        await writer.drain()
        frame = await _read_frame(reader)
        assert frame["type"] == "status_snapshot"
        tasks = {(t["platform"], t["thread_key"]): t for t in frame["tasks"]}  # type: ignore[attr-defined]
        assert ("cli", "cli:main") in tasks

        budget_entries = {b["currency"]: b for b in frame["budget"]}  # type: ignore[attr-defined]
        assert budget_entries[usage.PREMIUM_REQUESTS]["spent"] == 50.0
        assert budget_entries[usage.PREMIUM_REQUESTS]["cap"] == 200.0

        schedules = frame["schedules"]
        assert len(schedules) == 1  # type: ignore[arg-type]
        assert schedules[0]["action_type"] == ACTION_MESSAGE  # type: ignore[index]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_status_with_nothing_configured_returns_empty_lists(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(status_frame()).encode() + b"\n")
        await writer.drain()
        frame = await _read_frame(reader)
        assert frame["type"] == "status_snapshot"
        assert frame["budget"] == []
        assert frame["schedules"] == []
        assert frame["tasks"] == []
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_skills_returns_composed_skill_names(
    cli_stack: Callable[..., Any],
) -> None:
    server, _io = await cli_stack(
        [],
        skills_enabled=True,
        skills_plugin_path="vendor/chief-skills",
        default_skills=("setup-morning-brief",),
    )
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(skills_frame()).encode() + b"\n")
        await writer.drain()
        frame = await _read_frame(reader)
        assert frame["type"] == "skills_list"
        assert "setup-morning-brief" in frame["skills"]  # type: ignore[operator]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def test_skills_empty_when_disabled(cli_stack: Callable[..., Any]) -> None:
    server, _io = await cli_stack([])
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"
        writer.write(json.dumps(skills_frame()).encode() + b"\n")
        await writer.drain()
        frame = await _read_frame(reader)
        assert frame["type"] == "skills_list"
        assert frame["skills"] == []
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
