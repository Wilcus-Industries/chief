"""End-to-end tests for the #133 broadcast bus (mirror the chat stacks' outbound).

The central mechanism — a *second* platform stack (``platform="telegram"``) streaming a
real ``TaskManager`` turn onto a live client-plane socket as platform-tagged frames
while the platform bot still receives its unchanged text — is exercised for real: a real
:class:`SocketServer`, a real :class:`MirrorTaskIO` wrapping a real
:class:`TelegramTaskIO`, a real ``TaskManager`` on ``platform="telegram"``, real socket
clients, and the real ``message_log`` table. The only fakes are the Telegram bot seam
(``AsyncMock``, as in ``test_telegram``) and the LLM (``FakeSession`` from
``test_tasks``).

Each test builds its stack's engine io **exactly as ``app.build_*_stack`` does** — chat
stacks mirrored over a real :class:`MessageLog`, the CLI stack an unwrapped
:class:`CliTaskIO` over that same real log — so the one-row-per-outbound guarantee is
asserted against production wiring, not a lighter test-only construction.
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

from chief.adapters.base import BudgetCard
from chief.adapters.cli import CLI_LIMIT, CliTaskIO
from chief.adapters.mirror import MirrorTaskIO
from chief.adapters.telegram import TELEGRAM_LIMIT, TelegramTaskIO
from chief.client_plane import SocketServer
from chief.core.session import Milestone
from chief.core.tasks import SessionProto, TaskManager
from chief.gate.approvals import ApprovalCard
from chief.persistence.messages import KIND_CARD, KIND_FILE, ROLE_CHIEF, MessageLog
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
    """Bring up a real socket + an engine stack; test supplies the engine io + fake LLM.

    ``make_io`` receives the live server and the shared real ``MessageLog`` and returns
    the engine's ``TaskIO`` — the test's chance to reproduce its stack's production
    wiring (mirrored for chat, bare ``CliTaskIO`` for the CLI).
    """
    built: list[tuple[SocketServer, asyncio.Task[None], TaskManager]] = []

    async def _build(
        make_io: Callable[[SocketServer, MessageLog], object],
        *,
        platform: str,
        sessions: Sequence[FakeSession],
        message_limit: int,
    ) -> tuple[SocketServer, TaskManager]:
        server = SocketServer(str(tmp_path / f"{platform}.sock"))
        task = asyncio.create_task(server.run())
        await asyncio.wait_for(server.started.wait(), 5)
        manager = _manager(
            session_factory,
            make_io(server, MessageLog(session_factory)),
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
        result = await session.execute(
            select(MessageLogEntry).order_by(MessageLogEntry.id)
        )
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
        lambda s, log: MirrorTaskIO(
            TelegramTaskIO(bot), platform="telegram", log=log, server=s
        ),
        platform="telegram",
        sessions=[session],
        message_limit=TELEGRAM_LIMIT,
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

        # Exactly two rows — one per outbound, no duplicates — and both ROLE_CHIEF, the
        # same outbound role the CLI recorder writes (one role per direction, #134).
        rows = await _rows(session_factory)
        assert [(r.platform, r.thread_key, r.role, r.kind) for r in rows] == [
            ("telegram", "-100:7", ROLE_CHIEF, "milestone"),
            ("telegram", "-100:7", ROLE_CHIEF, "reply"),
        ]
        # Payload-less mirror rows can never replay, so they must never join the
        # claim set — a future claim_replay(platform="telegram") must find nothing.
        assert all(r.delivered is True and r.payload is None for r in rows)
    finally:
        for w in (w1, w2):
            w.close()
            with suppress(OSError):
                await w.wait_closed()


async def test_cli_stack_logged_once_without_frame_duplication(
    running_manager: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The CLI stack is NOT mirrored: its CliTaskIO is already the broadcaster and the
    # recorder, so one outbound yields exactly ONE frame and exactly ONE row. Built the
    # way app.build_cli_stack builds it — bare CliTaskIO over the real MessageLog — so a
    # regression that re-wrapped it in MirrorTaskIO would double the row and fail here.
    server, manager = await running_manager(
        lambda s, log: CliTaskIO(s, log=log),
        platform="cli",
        sessions=[FakeSession(model="m")],
        message_limit=CLI_LIMIT,
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

        # One row, and it is #132's replay-shaped row: ROLE_CHIEF (the same outbound
        # role the mirror writes) with the wire frame kept as its payload.
        (row,) = await _rows(session_factory)
        assert (row.platform, row.role, row.kind, row.text) == (
            "cli",
            ROLE_CHIEF,
            "reply",
            "reply:hi",
        )
        assert row.payload is not None and json.loads(row.payload) == reply
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
        self.edits: list[tuple[str, str]] = []
        self.budget_cards: list[tuple[str, object]] = []

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

    async def edit_card(self, msg_ref: str, text: str) -> None:
        self.edits.append((msg_ref, text))

    async def send_budget_card(self, route: str, card: object) -> None:
        self.budget_cards.append((route, card))


class _RecordingServer:
    """A SocketServer stand-in that records broadcast frames (no real socket)."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []

    async def broadcast(self, frame: dict[str, object]) -> None:
        self.frames.append(dict(frame))


async def test_thread_lifecycle_and_budget_are_not_mirrored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #136 narrows this: cards now mirror (see the tests below), but thread lifecycle
    # and budget traffic still don't — neither is owner-visible chat traffic.
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=server,  # type: ignore[arg-type]
    )

    assert await mirror.create_thread(like_thread_key="k", title="t") == "made:thread"
    await mirror.archive_thread("-100:1")
    await mirror.send_budget_card(
        "-100:1", BudgetCard(cycle="daily", text="budget?")
    )

    assert server.frames == []  # no broadcast for delegation-only methods
    assert await _rows(session_factory) == []  # and no log rows


async def test_send_card_mirrors_platform_tagged_card_frame_and_logs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #136 central mechanism: send_card delivers to the platform first (its ref is the
    # return value), then broadcasts a platform-tagged card frame + records a KIND_CARD
    # row.
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=server,  # type: ignore[arg-type]
    )
    card = ApprovalCard(approval_id=1, text="run it?")

    ref = await mirror.send_card("-100:1", card)

    assert ref == "-100:1|ref"  # the inner (platform) ref, unchanged
    assert inner.cards == [("-100:1", card)]
    (frame,) = server.frames
    assert frame["type"] == "card"
    assert frame["platform"] == "telegram"
    assert frame["thread_key"] == "-100:1"
    assert frame["approval_id"] == 1
    assert frame["text"] == "run it?"
    (row,) = await _rows(session_factory)
    assert (row.platform, row.kind, row.text) == ("telegram", KIND_CARD, "run it?")


async def test_edit_card_mirrors_card_resolved_frame_with_same_approval_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=server,  # type: ignore[arg-type]
    )
    card = ApprovalCard(approval_id=1, text="run it?")
    ref = await mirror.send_card("-100:1", card)
    server.frames.clear()  # only care about the edit's frame now

    await mirror.edit_card(ref, "✅ Approved (once) — by 42")

    assert inner.edits == [(ref, "✅ Approved (once) — by 42")]
    (frame,) = server.frames
    assert frame["type"] == "card_resolved"
    assert frame["platform"] == "telegram"
    assert frame["approval_id"] == 1
    assert frame["text"] == "✅ Approved (once) — by 42"


async def test_edit_card_unknown_ref_still_edits_platform_but_skips_mirror(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A card posted before a restart / re-armed with no memo: the edit still happens on
    # the platform, but there is no approval id to name on the socket.
    inner = _RecordingInner()
    server = _RecordingServer()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=server,  # type: ignore[arg-type]
    )

    await mirror.edit_card("unknown-ref", "⌛ Timed out — denied.")

    assert inner.edits == [("unknown-ref", "⌛ Timed out — denied.")]
    assert server.frames == []
    assert await _rows(session_factory) == []


async def test_card_mirror_broadcast_failure_never_breaks_platform_edit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Containment (per _mirror's blanket except): a raising broadcast must not stop the
    # platform edit that already happened.
    class _RaisingServer:
        async def broadcast(self, frame: dict[str, object]) -> None:
            raise RuntimeError("socket down")

    inner = _RecordingInner()
    mirror = MirrorTaskIO(
        inner,
        platform="telegram",
        log=MessageLog(session_factory),
        server=_RaisingServer(),  # type: ignore[arg-type]
    )
    card = ApprovalCard(approval_id=1, text="run it?")
    ref = await mirror.send_card("-100:1", card)  # must not raise

    await mirror.edit_card(ref, "outcome")  # must not raise

    assert inner.cards == [("-100:1", card)]
    assert inner.edits == [(ref, "outcome")]


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
        log=MessageLog(boom),  # type: ignore[arg-type]
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
        log=MessageLog(session_factory),
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
        KIND_FILE,
        "a caption",
        "reply.md",
    )
    assert row.delivered is True  # payload-less: never replayable, never claimable
