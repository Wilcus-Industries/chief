"""End-to-end tests for the #133 broadcast bus (mirror every stack's outbound).

The central mechanism — a *second* platform stack (``platform="telegram"``) streaming a
real ``TaskManager`` turn onto a live client-plane socket as platform-tagged frames
while the platform bot still receives its unchanged text — is exercised for real: a real
:class:`SocketServer`, a real :class:`MirrorTaskIO` wrapping a real
:class:`TelegramTaskIO`, a real ``TaskManager`` on ``platform="telegram"``, real socket
clients, and the real ``message_log`` table. The only fakes are the Telegram bot seam
(``AsyncMock``, as in ``test_telegram``) and the LLM (``FakeSession`` from
``test_tasks``).
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.cli import CLI_LIMIT, CliTaskIO
from chief.adapters.mirror import MirrorTaskIO
from chief.adapters.telegram import TELEGRAM_LIMIT, TelegramTaskIO
from chief.client_plane import SocketServer
from chief.core.session import Milestone
from chief.core.tasks import SessionProto, TaskManager
from chief.gate.approvals import ApprovalCard
from chief.persistence import message_log
from chief.persistence.models import MessageLogEntry
from test_tasks import FakeSession

Factory = Callable[..., SessionProto]


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, object]:
    line = await asyncio.wait_for(reader.readline(), 5)
    frame: dict[str, object] = json.loads(line)
    return frame


async def _no(*args: Any, **kwargs: Any) -> bool:
    return False


def _seq_factory(sessions: Sequence[FakeSession]) -> Factory:
    it = iter(sessions)

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        session = next(it)
        session.model = model
        session.resume = resume
        return session

    return factory


def _manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: object,
    *,
    platform: str,
    factory: Factory,
    message_limit: int,
) -> TaskManager:
    """A real engine on ``platform`` over ``io``, LLM stubbed by ``factory``."""
    return TaskManager(
        session_factory=session_factory,
        io=io,  # type: ignore[arg-type]
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        platform=platform,
        concurrency=3,
        turn_timeout=1000.0,
        idle_archive_seconds=1000.0,
        compaction_idle_seconds=1000.0,
        message_limit=message_limit,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
    )


@pytest.fixture
async def running_manager(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Callable[..., Any]]:
    """Bring up a real socket + a mirrored engine stack; test supplies the fake LLM."""
    built: list[tuple[SocketServer, asyncio.Task[None], TaskManager]] = []

    async def _build(
        make_inner: Callable[[SocketServer], object],
        *,
        platform: str,
        sessions: Sequence[FakeSession],
        message_limit: int,
        server_for_mirror: bool,
    ) -> tuple[SocketServer, TaskManager]:
        server = SocketServer(str(tmp_path / f"{platform}.sock"))
        task = asyncio.create_task(server.run())
        await asyncio.wait_for(server.started.wait(), 5)
        io = MirrorTaskIO(
            make_inner(server),  # type: ignore[arg-type]
            platform=platform,
            session_factory=session_factory,
            server=server if server_for_mirror else None,
        )
        manager = _manager(
            session_factory,
            io,
            platform=platform,
            factory=_seq_factory(sessions),
            message_limit=message_limit,
        )
        built.append((server, task, manager))
        return server, manager

    try:
        yield _build
    finally:
        for server, task, manager in built:
            await manager.shutdown()
            await server.stop()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[MessageLogEntry]:
    async with session_factory() as session:
        result = await session.execute(select(MessageLogEntry))
        return list(result.scalars().all())


async def test_second_stack_mirrors_onto_two_clients_and_logs(
    running_manager: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The central mechanism: a telegram stack streams a task's milestone + reply onto
    # the socket as platform="telegram" frames to BOTH connected clients, the bot
    # receives its unchanged text (prefix intact), and both rows land in message_log.
    bot = AsyncMock()
    session = FakeSession(model="m", milestones=[Milestone(text="using Bash")])
    server, manager = await running_manager(
        lambda _s: TelegramTaskIO(bot),
        platform="telegram",
        sessions=[session],
        message_limit=TELEGRAM_LIMIT,
        server_for_mirror=True,
    )
    r1, w1 = await asyncio.open_unix_connection(server.path)
    r2, w2 = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(r1))["type"] == "hello"
        assert (await _read_frame(r2))["type"] == "hello"

        await manager.dispatch(thread_key="-100:7", text="hi")

        for reader in (r1, r2):
            milestone = await _read_frame(reader)
            assert milestone["type"] == "milestone"
            assert milestone["platform"] == "telegram"
            assert milestone["thread_key"] == "-100:7"
            assert milestone["text"] == "using Bash"  # the "· " prefix is stripped

            reply = await _read_frame(reader)
            assert reply["type"] == "reply"
            assert reply["platform"] == "telegram"
            assert reply["thread_key"] == "-100:7"
            assert reply["text"] == "reply:hi"

        # Platform delivery unchanged: the bot got the milestone WITH its "· " prefix
        # and the reply text (chat_id -100, thread 7 parsed from "-100:7").
        sent = [c.kwargs["text"] for c in bot.send_message.await_args_list]
        assert "· using Bash" in sent
        assert "reply:hi" in sent

        rows = await _rows(session_factory)
        assert {(r.platform, r.thread_key, r.role, r.kind) for r in rows} == {
            ("telegram", "-100:7", message_log.ROLE_ASSISTANT, "milestone"),
            ("telegram", "-100:7", message_log.ROLE_ASSISTANT, "reply"),
        }
    finally:
        for w in (w1, w2):
            w.close()
            with suppress(OSError):
                await w.wait_closed()


async def test_cli_stack_logged_without_frame_duplication(
    running_manager: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The CLI stack's inner CliTaskIO already broadcasts, so the mirror runs with
    # server=None: a connected client sees exactly ONE reply frame (no double
    # broadcast), while the message log still records the cli row.
    server, manager = await running_manager(
        lambda s: CliTaskIO(s),
        platform="cli",
        sessions=[FakeSession(model="m")],
        message_limit=CLI_LIMIT,
        server_for_mirror=False,
    )
    reader, writer = await asyncio.open_unix_connection(server.path)
    try:
        assert (await _read_frame(reader))["type"] == "hello"

        await manager.dispatch(thread_key="cli:main", text="hi")

        reply = await _read_frame(reader)
        assert reply["type"] == "reply"
        assert reply["platform"] == "cli"
        assert reply["text"] == "reply:hi"

        # No duplicate frame: a second read finds nothing before the timeout.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), 0.3)

        rows = await _rows(session_factory)
        assert [(r.platform, r.kind, r.text) for r in rows] == [
            ("cli", "reply", "reply:hi")
        ]
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


class _RecordingInner:
    """A structural PlatformIO that records calls and returns canned results."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, bytes, str | None]] = []
        self.created: list[tuple[str, str]] = []
        self.cards: list[tuple[str, object]] = []

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

    async def send_file(
        self, thread_key: str, filename: str, data: bytes, caption: str | None = None
    ) -> None:
        self.files.append((thread_key, filename, data, caption))

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        self.created.append((like_thread_key, title))
        return "made:thread"

    async def archive_thread(self, thread_key: str) -> None: ...

    async def send_card(self, route: str, card: object) -> str:
        self.cards.append((route, card))
        return f"{route}|ref"

    async def edit_card(self, msg_ref: str, text: str) -> None: ...
    async def send_budget_card(self, route: str, card: object) -> None: ...


class _RecordingServer:
    """A SocketServer stand-in that records broadcast frames (no real socket)."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    async def broadcast(self, frame: dict[str, object]) -> None:
        self.frames.append(dict(frame))


async def test_delegation_methods_are_not_mirrored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        session_factory=session_factory,
        server=server,  # type: ignore[arg-type]
    )

    card = ApprovalCard(approval_id=1, text="run it?")
    assert await mirror.create_thread(like_thread_key="k", title="t") == "made:thread"
    assert await mirror.send_card("-100:1", card) == "-100:1|ref"

    assert server.frames == []  # no broadcast for delegation-only methods
    assert await _rows(session_factory) == []  # and no log rows


async def test_mirror_failure_never_breaks_platform_delivery(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A session_factory that raises on call: inner.send still delivers, and no exception
    # escapes send (mirroring is a contained best-effort side channel).
    def boom() -> AsyncSession:
        raise RuntimeError("db down")

    inner = _RecordingInner()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        session_factory=boom,  # type: ignore[arg-type]
        server=None,
    )

    await mirror.send("-100:1", "answer")  # must not raise

    assert inner.sends == [("-100:1", "answer")]  # platform delivery happened


async def test_send_file_broadcasts_and_logs_without_bytes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="discord",
        session_factory=session_factory,
        server=server,  # type: ignore[arg-type]
    )

    await mirror.send_file("-100:2", "reply.md", b"payload", "a caption")

    assert inner.files == [("-100:2", "reply.md", b"payload", "a caption")]
    (frame,) = server.frames
    assert frame["type"] == "file"
    assert frame["platform"] == "discord"
    assert base64.b64decode(str(frame["data"])) == b"payload"

    (row,) = await _rows(session_factory)
    assert (row.platform, row.kind, row.text, row.filename) == (
        "discord",
        message_log.KIND_FILE,
        "a caption",
        "reply.md",
    )
