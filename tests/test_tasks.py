"""TaskManager: hybrid grace, steering, interrupt, semaphore, idle, recovery, spawn."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext
from copilot import ProviderConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import FILE_REPLY_NOTE, Attachment, Surface
from chief.core.agent import NO_REPLY
from chief.core.routing import RoutingStore
from chief.core.session import Final, Milestone, TurnEvent
from chief.core.tasks import (
    GROUP_MODE_NOTE,
    MEMORY_TOOLS,
    PAUSED_BUDGET_ACK,
    TURN_TIMEOUT_NOTE,
    WEB_META_TOOLS,
    SessionProto,
    TaskManager,
)
from chief.gate.approvals import OPUS_ESCALATION_KIND
from chief.gate.policy import PolicyStore
from chief.memory.store import Fact
from chief.memory.versioning import GitVersioner, NullVersioner, Versioner
from chief.obs.audit import AuditLog
from chief.persistence.tasks import (
    CANCELLED,
    DONE,
    FAILED,
    OPEN,
    RUNNING,
    get_or_create_task,
    get_task,
    set_session_id,
    set_status,
)
from chief.tools.calendar import mcp as calendar_mcp
from chief.tools.gmail import mcp as gmail_mcp
from chief.tools.guest import GuestAdminService
from chief.tools.schedule import ScheduleBashService, ScheduleService
from chief.tools.shell import ShellService

Factory = Callable[..., SessionProto]


class FakeMemory:
    """Minimal MemoryStore stub for system prompt assembly and session wiring."""

    def __init__(self) -> None:
        self._versioner: Versioner = NullVersioner()

    @property
    def versioner(self) -> Versioner:
        return self._versioner

    def facts_listing(self) -> str:
        return "- facts/owner/x.md — X"

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return "# Will"

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        raise NotImplementedError


class FakeSession:
    """Structural TaskSession: run_turn blocks on an optional gate before Final."""

    def __init__(
        self,
        *,
        model: str,
        resume: str | None = None,
        gate: asyncio.Event | None = None,
        on_start: Callable[[], None] | None = None,
        milestones: list[Milestone] | None = None,
        after_gate: list[Milestone] | None = None,
        cost: float = 0.0,
        rate_limit: str | None = None,
    ) -> None:
        self.model = model
        self.resume = resume
        self.session_id = resume
        self.queries: list[str] = []
        self.attachments_seen: list[tuple[Attachment, ...]] = []
        self.interrupted = False
        self.closed = False
        self.last_cost_usd = 0.0
        self.last_rate_limit_status: str | None = None
        #: Simulated served model (#79): defaults to the model the session was spawned
        #: on, so a routed session reports the target it actually ran (set in run_turn).
        self.last_served_model: str | None = None
        self._gate = gate
        self._on_start = on_start
        self._milestones = milestones or []
        self._after_gate = after_gate or []
        self._cost = cost
        self._rate_limit = rate_limit

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        self.queries.append(text)
        self.attachments_seen.append(tuple(attachments))
        self.last_cost_usd = 0.0  # this turn's spend only (mirrors TaskSession)
        if self._on_start is not None:
            self._on_start()
        for milestone in self._milestones:
            yield milestone
        if self._gate is not None:
            await self._gate.wait()
        self.session_id = f"sess-{text}"
        self.last_cost_usd = self._cost
        self.last_rate_limit_status = self._rate_limit
        self.last_served_model = self.model  # the target this session actually ran on
        for milestone in self._after_gate:  # yielded post-gate (after a cancel flips)
            yield milestone
        yield Final(text=f"reply:{text}")

    async def interrupt(self) -> None:
        self.interrupted = True
        if self._gate is not None:
            self._gate.set()

    async def set_model(self, model: str) -> None:
        self.model = model

    async def aclose(self) -> None:
        self.closed = True


class FakeBudget:
    """Structural BudgetGate: records spend, serves a fixed mode (M9 enforcement)."""

    def __init__(self, *, mode_value: str = "normal") -> None:
        self._mode = mode_value
        self.recorded: list[float] = []
        self.rate_limited = 0

    async def record(self, cost: float) -> None:
        self.recorded.append(cost)

    async def note_rate_limited(self) -> None:
        self.rate_limited += 1

    async def mode(self) -> str:
        return self._mode


class FakeApprovals:
    """Structural ApprovalManager: returns a fixed decision, records each request."""

    def __init__(self, *, approve: bool = True) -> None:
        self._approve = approve
        self.requests: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> bool:
        self.requests.append(kwargs)
        return self._approve


class FakeIO:
    def __init__(self, next_thread: str = "-100:99") -> None:
        self.sends: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, bytes, str | None]] = []
        self.created: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self._next = next_thread

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        self.files.append((thread_key, filename, data, caption))

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        self.created.append((like_thread_key, title))
        return self._next

    async def archive_thread(self, thread_key: str) -> None:
        self.archived.append(thread_key)


async def _no(*args: Any, **kwargs: Any) -> bool:
    return False


async def _yes(*args: Any, **kwargs: Any) -> bool:
    return True


def _one(session: FakeSession) -> Factory:
    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        session.model = model
        session.resume = resume
        return session

    return factory


def _manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: Any,
    *,
    factory: Factory,
    stop: Callable[..., Any] = _no,
    warrants: Callable[..., Any] = _no,
    concurrency: int = 3,
    turn_timeout: float = 1000.0,
    idle: float = 1000.0,
    compaction: float = 1000.0,
    message_limit: int = 4096,
    budget: Any = None,
    owner_inbox: str | None = None,
    downgrade_model: str | None = None,
    approvals: Any = None,
    opus_auto_detect: bool = False,
    is_complex: Callable[..., Any] = _no,
    owner_model_opus: str = "claude-opus-4-8",
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        concurrency=concurrency,
        turn_timeout=turn_timeout,
        idle_archive_seconds=idle,
        compaction_idle_seconds=compaction,
        message_limit=message_limit,
        session_factory_sdk=factory,
        stop_intent=stop,
        warrants_task=warrants,
        budget=budget,
        owner_inbox=owner_inbox,
        budget_downgrade_model=downgrade_model,
        approvals=approvals,
        opus_auto_detect=opus_auto_detect,
        is_complex=is_complex,
        owner_model_opus=owner_model_opus,
    )


async def _until(pred: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


async def test_turn_replies_inline_without_ack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m", milestones=[Milestone(text="using Bash")])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    assert ("-100:5", "· using Bash") in io.sends  # milestone posted
    # No "working on it…" ack on any surface — the auto-message is gone.
    assert all("working on it" not in text for _, text in io.sends)
    await mgr.shutdown()


async def test_dispatch_threads_attachments_into_run_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess))
    att = Attachment(media_type="image/png", data=b"PNG", filename=None)

    await mgr.dispatch(thread_key="-100:5", text="what is this?", attachments=(att,))
    await _until(lambda: ("-100:5", "reply:what is this?") in io.sends)

    # The owner's media rides the queued Turn through to the session's run_turn.
    assert sess.attachments_seen == [(att,)]
    await mgr.shutdown()


async def test_dispatch_pre_extracts_pdf_to_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #81: an incoming PDF is read as extracted text at the dispatch seam — the PDF is
    # folded into the turn text (backend-agnostic) and no PDF attachment reaches the
    # session, so neither backend has to carry a PDF content block.
    from test_pdf import make_pdf

    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess))
    pdf = Attachment(
        media_type="application/pdf",
        data=make_pdf("Board meeting notes"),
        filename="notes.pdf",
    )

    await mgr.dispatch(thread_key="-100:5", text="summarize", attachments=(pdf,))
    await _until(lambda: bool(sess.queries))

    assert "summarize" in sess.queries[0]
    assert "Board meeting notes" in sess.queries[0]  # real pypdf extraction
    assert sess.attachments_seen == [()]  # the PDF was consumed into text
    await mgr.shutdown()


async def test_long_reply_sent_as_file(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), message_limit=20)

    await mgr.dispatch(thread_key="-100:5", text="x" * 100)
    await _until(lambda: len(io.files) == 1)

    thread_key, filename, data, caption = io.files[0]
    assert thread_key == "-100:5"
    assert filename.endswith(".md")
    assert data == f"reply:{'x' * 100}".encode()
    assert caption == FILE_REPLY_NOTE
    assert io.sends == []  # delivered as a file, not also as text
    await mgr.shutdown()


async def test_short_reply_sent_as_text_not_file(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), message_limit=4096)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    assert io.files == []  # short reply stays inline
    await mgr.shutdown()


# ---- per-block reply streaming (issue #64) -----------------------------------


class MultiBlockSession(FakeSession):
    """A session that yields one Final per entry in ``blocks``, interleaved with
    ``milestones`` and then ``after_milestones``.  Models the new per-block stream
    the real TaskSession now produces.
    """

    def __init__(
        self,
        *,
        model: str,
        blocks: list[str],
        milestones: list[Milestone] | None = None,
        after_milestones: list[Milestone] | None = None,
    ) -> None:
        super().__init__(model=model)
        self._blocks = blocks
        self._milestones_before = milestones or []
        self._milestones_after = after_milestones or []

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        self.queries.append(text)
        self.attachments_seen.append(tuple(attachments))
        self.last_cost_usd = 0.0
        for ms in self._milestones_before:
            yield ms
        for block_text in self._blocks:
            yield Final(text=block_text)
        for ms in self._milestones_after:
            yield ms
        self.session_id = f"sess-{text}"


async def test_owner_dm_multi_block_sends_each_block_immediately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Owner DM turn with multiple text blocks delivers each as a separate message."""
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["first block", "second block", "third"])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "third") in io.sends)

    # Each block is a separate send, in order, with no joining.
    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert "first block" in block_sends
    assert "second block" in block_sends
    assert "third" in block_sends
    assert block_sends.index("first block") < block_sends.index("second block")
    assert block_sends.index("second block") < block_sends.index("third")
    await mgr.shutdown()


async def test_owner_home_multi_block_sends_each_block_immediately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An owner HOME turn delivers each block separately (same as DM)."""
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["block A", "block B"])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.HOME)
    await _until(lambda: ("-100:5", "block B") in io.sends)

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert "block A" in block_sends
    assert "block B" in block_sends
    assert block_sends.index("block A") < block_sends.index("block B")
    await mgr.shutdown()


async def test_owner_single_block_still_one_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A plain no-tool turn with one text block still arrives as a single message."""
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["just one reply"])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="hi", surface=Surface.DM)
    await _until(lambda: ("-100:5", "just one reply") in io.sends)

    assert [t for k, t in io.sends if k == "-100:5"] == ["just one reply"]
    await mgr.shutdown()


async def test_owner_milestones_interleaved_with_blocks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Milestone lines appear interleaved with per-block Final deliveries."""
    io = FakeIO()
    sess = MultiBlockSession(
        model="m",
        blocks=["result text"],
        milestones=[Milestone(text="using Bash")],
    )
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "result text") in io.sends)

    thread_sends = [t for k, t in io.sends if k == "-100:5"]
    assert "· using Bash" in thread_sends
    assert "result text" in thread_sends
    # milestone comes before the block text (milestones emit before Finals here)
    assert thread_sends.index("· using Bash") < thread_sends.index("result text")
    await mgr.shutdown()


async def test_group_owner_turn_streams_each_block_as_separate_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A GROUP owner turn delivers each text block as its own message (issue #67)."""
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["part one", "part two"])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:grp", text="go", surface=Surface.GROUP)
    await _until(lambda: ("-100:grp", "part two") in io.sends)

    # Each block is a separate send, in order — NOT joined into one message.
    group_sends = [t for k, t in io.sends if k == "-100:grp"]
    assert "part one" in group_sends
    assert "part two" in group_sends
    assert group_sends.index("part one") < group_sends.index("part two")
    # They must be distinct sends (not concatenated).
    assert not any("part one" in t and "part two" in t for t in group_sends)
    await mgr.shutdown()


async def test_group_owner_turn_streams_per_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GROUP owner turns stream each block as a separate message."""
    io = FakeIO()
    sess = MultiBlockSession(
        model="m",
        blocks=["group reply"],
        milestones=[Milestone(text="using Bash")],
    )
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:grp", text="go", surface=Surface.GROUP)
    await _until(lambda: ("-100:grp", "group reply") in io.sends)

    # The reply itself must arrive per-block.
    group_sends = [t for k, t in io.sends if k == "-100:grp"]
    assert "group reply" in group_sends
    await mgr.shutdown()


async def test_guest_dm_turn_accumulates_blocks_into_one_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guest DM turn accumulates all blocks into one joined message (unchanged)."""
    io = FakeIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = MultiBlockSession(model=model, blocks=["guest A", "guest B"])
        sessions.append(sess)
        return sess

    mgr = _manager(session_factory, io, factory=factory)

    await mgr.dispatch_guest(thread_key="555:0", text="hi", from_label="Alice")
    await _until(lambda: any("guest A" in t for _, t in io.sends))

    guest_sends = [t for k, t in io.sends if k == "555:0"]
    # Both blocks joined into one send.
    assert sum(1 for t in guest_sends if "guest A" in t) == 1
    content_send = next(t for t in guest_sends if "guest A" in t)
    assert "guest B" in content_send
    await mgr.shutdown()


async def test_owner_per_block_transcript_records_each_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each delivered block is recorded in the task transcript (issue #64)."""
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["block1", "block2"])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "block2") in io.sends)

    task = mgr._tasks["-100:5"]
    chief_entries = [text for role, text in task.transcript if role == "chief"]
    assert "block1" in chief_entries
    assert "block2" in chief_entries
    await mgr.shutdown()


# ---- per-block streaming: empty/whitespace block guard (issue #69) ------------


class StrictFakeIO(FakeIO):
    """FakeIO that raises on empty sends, mirroring Telegram/Discord behaviour."""

    async def send(self, thread_key: str, text: str) -> None:
        if not text.strip():
            raise ValueError(f"empty send on {thread_key!r}: {text!r}")
        await super().send(thread_key, text)


async def test_owner_per_block_whitespace_block_is_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Whitespace-only block alongside real text is silently dropped; turn succeeds."""
    io = StrictFakeIO()
    # SDK sometimes emits a trailing-newline block after a tool call.
    sess = MultiBlockSession(model="m", blocks=["real reply", "\n", "  "])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "real reply") in io.sends)

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert "real reply" in block_sends
    # Whitespace blocks must never reach the platform.
    assert "" not in block_sends
    assert "\n" not in block_sends
    assert "  " not in block_sends
    # The turn must not be marked FAILED.
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_owner_per_block_empty_block_is_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty-string block is silently dropped; the real text still arrives."""
    io = StrictFakeIO()
    sess = MultiBlockSession(model="m", blocks=["", "hello", ""])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "hello") in io.sends)

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert block_sends == ["hello"]
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_owner_per_block_all_whitespace_posts_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All-whitespace owner DM turn posts nothing.

    Whitespace-only blocks are stripped and discarded; owner home/DM turns send no
    NO_REPLY ack (only GROUP does), so the turn completes silently.
    """
    io = StrictFakeIO()
    sess = MultiBlockSession(model="m", blocks=["\n", "   "])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    # Wait for the turn to settle — generating=False means the consumer exited cleanly.
    await _until(lambda: not mgr._tasks["-100:5"].generating)
    await asyncio.sleep(0.05)  # let the consumer fully drain

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    # Nothing should be posted: no NO_REPLY ack for owner home/DM turns.
    assert block_sends == []
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_owner_per_block_no_text_blocks_posts_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tool-only owner DM turn with zero Final events posts nothing.

    Owner home/DM turns send no NO_REPLY ack (only GROUP does), so a turn that
    produces no text blocks completes silently.
    """
    io = StrictFakeIO()
    sess = MultiBlockSession(model="m", blocks=[])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: not mgr._tasks["-100:5"].generating)
    await asyncio.sleep(0.05)

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert block_sends == []
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_group_empty_turn_posts_no_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tool-only GROUP turn posts exactly one NO_REPLY.

    Issue #70: the branch ``if per_block_sent == 0 and task.surface is Surface.GROUP``
    must fire for GROUP surface (per_block=True) and deliver NO_REPLY so the owner
    gets an acknowledgement in the group thread.
    """
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=[])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:grp", text="go", surface=Surface.GROUP)
    await _until(lambda: ("-100:grp", NO_REPLY) in io.sends)

    group_sends = [t for k, t in io.sends if k == "-100:grp"]
    assert group_sends == [NO_REPLY]
    await mgr.shutdown()


async def test_group_whitespace_only_turn_posts_no_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All-whitespace GROUP turn posts exactly one NO_REPLY.

    Issue #70: whitespace-only blocks are stripped and discarded (per_block_sent stays
    zero), so the same NO_REPLY branch fires as for a tool-only turn.
    """
    io = FakeIO()
    sess = MultiBlockSession(model="m", blocks=["\n", "   "])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:grp", text="go", surface=Surface.GROUP)
    await _until(lambda: ("-100:grp", NO_REPLY) in io.sends)

    group_sends = [t for k, t in io.sends if k == "-100:grp"]
    assert group_sends == [NO_REPLY]
    await mgr.shutdown()


async def test_owner_per_block_stripped_text_is_sent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-empty block with surrounding whitespace is sent stripped."""
    io = StrictFakeIO()
    sess = MultiBlockSession(model="m", blocks=["  trimmed reply  "])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go", surface=Surface.DM)
    await _until(lambda: ("-100:5", "trimmed reply") in io.sends)

    block_sends = [t for k, t in io.sends if k == "-100:5"]
    assert block_sends == ["trimmed reply"]
    await mgr.shutdown()


async def test_slow_group_turn_stays_silent_then_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GROUP owner turns post no "working on it…" ack; the reply still arrives."""
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate)
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:grp", text="slow", surface=Surface.GROUP)
    # Let the turn start and block on the gate, then assert no ack was posted.
    await _until(lambda: bool(sess.queries))
    assert all("working on it" not in text for _, text in io.sends)
    assert ("-100:grp", "reply:slow") not in io.sends

    gate.set()
    await _until(lambda: ("-100:grp", "reply:slow") in io.sends)
    await mgr.shutdown()


async def test_midturn_message_queues_when_not_stop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate)
    mgr = _manager(session_factory, io, factory=_one(sess), stop=_no)

    await mgr.dispatch(thread_key="-100:5", text="first")
    await _until(lambda: sess.queries == ["first"])
    await mgr.dispatch(thread_key="-100:5", text="second")

    assert sess.interrupted is False
    gate.set()
    await _until(lambda: sess.queries == ["first", "second"])
    await mgr.shutdown()


async def test_midturn_stop_interrupts_then_runs_new(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate)
    mgr = _manager(session_factory, io, factory=_one(sess), stop=_yes)

    await mgr.dispatch(thread_key="-100:5", text="long")
    await _until(lambda: sess.queries == ["long"])
    await mgr.dispatch(thread_key="-100:5", text="stop, do X")

    await _until(lambda: sess.interrupted is True)
    await _until(lambda: sess.queries == ["long", "stop, do X"])
    await mgr.shutdown()


async def test_semaphore_bounds_generating_turns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    started = {"n": 0}

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        return FakeSession(
            model=model,
            resume=resume,
            gate=gate,
            on_start=lambda: started.__setitem__("n", started["n"] + 1),
        )

    mgr = _manager(session_factory, io, factory=factory, concurrency=2)

    for i in range(3):
        await mgr.dispatch(thread_key=f"-100:{i}", text="go")

    await _until(lambda: started["n"] == 2)
    await asyncio.sleep(0.05)
    assert started["n"] == 2  # third blocked on the semaphore

    gate.set()
    await _until(lambda: started["n"] == 3)
    await mgr.shutdown()


async def test_idle_archives_and_marks_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), idle=0.02)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: "-100:5" in io.archived)

    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == DONE
    assert sess.closed is True
    await mgr.shutdown()


async def test_idle_owner_task_does_not_trigger_distillation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An idle task archives without running any distillation pass (removed in #21)."""
    io = FakeIO()
    memory = FakeMemory()
    mgr = TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        idle_archive_seconds=0.02,
        session_factory_sdk=_one(FakeSession(model="m")),
        stop_intent=_no,
        warrants_task=_no,
        memory=memory,
        memory_dir="/tmp/mem",
        owner_name="Will",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: "-100:5" in io.archived)

    # No memory writes — distillation is gone, not merely skipped.
    assert not any("📝" in t for _, t in io.sends)
    await mgr.shutdown()


async def test_reopen_after_idle_resumes_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        sessions.append(sess)
        return sess

    mgr = _manager(session_factory, io, factory=factory, idle=0.02)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: "-100:5" in io.archived)

    # A message after archive reopens the task: a fresh session resumed from the
    # persisted id, ending OPEN (the idle DONE must not clobber the new turn).
    await mgr.dispatch(thread_key="-100:5", text="again")
    await _until(lambda: ("-100:5", "reply:again") in io.sends)

    assert len(sessions) == 2
    assert sessions[1].resume == "sess-hi"
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_reopen_awaits_inflight_teardown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    release = asyncio.Event()

    class BlockingIO(FakeIO):
        async def archive_thread(self, thread_key: str) -> None:
            self.archived.append(thread_key)
            await release.wait()  # hold the teardown open mid-archive

    io = BlockingIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        sessions.append(sess)
        return sess

    mgr = _manager(session_factory, io, factory=factory, idle=0.02)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: "-100:5" in io.archived)  # teardown started, blocked
    # The first idle timer has fired (its sleep elapsed). The reopened turn will re-arm
    # a fresh timer on completion; lengthen idle now so that one can't fire and archive
    # the just-reopened task before we assert (only the second arm sees the new value).
    mgr._idle_archive_seconds = 1000.0

    rt = mgr._tasks["-100:5"]
    assert rt.cancelled is True  # slot kept (cancelled) until teardown finishes

    reopen = asyncio.create_task(mgr.dispatch(thread_key="-100:5", text="again"))
    await asyncio.sleep(0.02)
    assert reopen.done() is False  # blocked awaiting the in-flight idle_handle

    release.set()
    await reopen  # teardown drains, the task reopens, the new turn is enqueued
    rt2 = mgr._tasks["-100:5"]
    # The reopened turn commits OPEN, then arms a fresh idle timer. Sync on that arm: an
    # in-memory edge landing after the OPEN commit. Polling the row instead would share
    # the StaticPool connection with the turn's write and could roll it back, stranding
    # the row at the prior RUNNING.
    await _until(lambda: rt2.idle_handle is not None)

    assert len(sessions) == 2  # one fresh session, no duplicate teardown
    assert sessions[1].resume == "sess-hi"  # resumed from the persisted id
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN  # reopened, not stuck DONE
    await mgr.shutdown()


async def test_milestone_suppressed_after_cancel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate, after_gate=[Milestone(text="late")])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="go")
    await _until(lambda: sess.queries == ["go"])  # turn started, awaiting gate

    mgr._tasks["-100:5"].cancelled = True  # cancel flag without tearing down
    gate.set()
    await _until(lambda: mgr._tasks["-100:5"].generating is False)

    assert ("-100:5", "· late") not in io.sends  # milestone after cancel suppressed
    assert ("-100:5", "reply:go") not in io.sends  # final dropped too
    await mgr.shutdown()


async def test_failed_turn_does_not_arm_idle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class BoomSession(FakeSession):
        async def run_turn(
            self, text: str, attachments: Sequence[Attachment] = ()
        ) -> AsyncIterator[TurnEvent]:
            self.queries.append(text)
            for _ in ():  # never iterates — keeps this an async generator
                yield Final(text="")
            raise RuntimeError("boom")

    io = FakeIO()
    sess = BoomSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), idle=0.02)

    await mgr.dispatch(thread_key="-100:5", text="go")
    await _until(lambda: ("-100:5", "⚠️ that task hit an error.") in io.sends)

    # The failed turn must not re-arm idle (which would relabel FAILED → DONE).
    assert mgr._tasks["-100:5"].idle_handle is None
    await asyncio.sleep(0.05)  # well past the idle window
    assert "-100:5" not in io.archived
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == FAILED
    await mgr.shutdown()


class HangSession(FakeSession):
    """A session whose turn never reaches a Final — the wedge the watchdog guards.

    The first turn blocks on a never-set event (no terminal result), so the engine's
    ``async for`` would hang forever without the per-turn timeout. Once the watchdog
    tears it down (``aclose``) it heals, modelling the real session reconnecting on a
    fresh CLI — so the same thread's next turn succeeds.
    """

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        if not self.closed:  # wedged until the watchdog disconnects us
            self.queries.append(text)
            await asyncio.Event().wait()  # never resolves — no terminal ResultMessage
        self.closed = False  # reconnected fresh, like TaskSession._ensure_connected
        async for event in super().run_turn(text, attachments):
            yield event


async def test_watchdog_times_out_wedged_turn_and_recovers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        # First session wedges; the next is a normal one to prove the slot was freed.
        sess: FakeSession = (
            HangSession(model=model) if not sessions else FakeSession(model=model)
        )
        sessions.append(sess)
        return sess

    # concurrency=1 so a leaked slot would block every later task entirely.
    mgr = _manager(
        session_factory, io, factory=factory, concurrency=1, turn_timeout=0.05
    )

    await mgr.dispatch(thread_key="-100:1", text="hang")
    await _until(lambda: ("-100:1", TURN_TIMEOUT_NOTE) in io.sends)

    # The watchdog reset generating and tore the wedged session down.
    assert mgr._tasks["-100:1"].generating is False
    assert sessions[0].closed is True
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:1")
    assert db is not None and db.status == FAILED

    # Same thread recovers: a follow-up turn on the reset session replies (FAILED→OPEN).
    await mgr.dispatch(thread_key="-100:1", text="retry")
    await _until(lambda: ("-100:1", "reply:retry") in io.sends)
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:1")
    assert db is not None and db.status == OPEN

    # The semaphore slot was released: a fresh task can also generate and reply.
    await mgr.dispatch(thread_key="-100:2", text="go")
    await _until(lambda: ("-100:2", "reply:go") in io.sends)
    await mgr.shutdown()


async def test_recover_pings_without_autoresume_then_resumes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        db = await get_or_create_task(
            session,
            platform="telegram",
            thread_key="-100:7",
            tier="owner",
            title="big job",
        )
        await set_session_id(session, db, "sess-prior")
        await set_status(session, db, RUNNING)

    io = FakeIO()
    captured: list[str | None] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        captured.append(resume)
        return FakeSession(model=model, resume=resume)

    mgr = _manager(session_factory, io, factory=factory)
    await mgr.recover()

    assert any(tk == "-100:7" and "interrupted" in txt for tk, txt in io.sends)
    assert captured == []  # no auto-resume
    async with session_factory() as session:
        reloaded = await get_task(session, platform="telegram", thread_key="-100:7")
    assert reloaded is not None and reloaded.status == OPEN

    await mgr.dispatch(thread_key="-100:7", text="resume please")
    await _until(lambda: captured == ["sess-prior"])
    await mgr.shutdown()


async def test_general_message_spawns_topic_when_warranted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO(next_thread="-100:77")
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), warrants=_yes)

    await mgr.dispatch(thread_key="-100:0", text="build me a thing", is_general=True)
    await _until(lambda: ("-100:77", "reply:build me a thing") in io.sends)

    assert io.created == [("-100:0", "build me a thing")]
    assert ("-100:0", "→ Tracking that in a new topic.") in io.sends
    await mgr.shutdown()


async def test_general_casual_replies_in_general(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), warrants=_no)

    await mgr.dispatch(thread_key="-100:0", text="hey thanks", is_general=True)
    await _until(lambda: ("-100:0", "reply:hey thanks") in io.sends)

    assert io.created == []
    await mgr.shutdown()


async def test_cancel_interrupts_and_marks_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate)
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="long task")
    await _until(lambda: sess.queries == ["long task"])

    assert await mgr.cancel("-100:5") is True
    assert sess.interrupted is True
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == CANCELLED
    await mgr.shutdown()


# ---- casual self-compaction + /branch ----------------------------------------


async def test_casual_general_message_is_marked_casual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), warrants=_no)

    await mgr.dispatch(thread_key="-100:0", text="hey", is_general=True)
    await _until(lambda: ("-100:0", "reply:hey") in io.sends)

    assert mgr._tasks["-100:0"].is_casual is True  # casual lane → self-compacts
    await mgr.shutdown()


async def test_spawned_topic_is_not_casual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO(next_thread="-100:77")
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess), warrants=_yes)

    await mgr.dispatch(thread_key="-100:0", text="build a thing", is_general=True)
    await _until(lambda: ("-100:77", "reply:build a thing") in io.sends)

    # A spawned topic is a real, full-memory task — it archives, never compacts.
    assert mgr._tasks["-100:77"].is_casual is False
    await mgr.shutdown()


async def test_casual_idle_compacts_and_reseeds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        sessions.append(sess)
        return sess

    mgr = _manager(
        session_factory, io, factory=factory, idle=1000.0, compaction=0.02
    )

    await mgr.dispatch(thread_key="-100:0", text="hi", is_general=True)
    await _until(lambda: ("-100:0", "reply:hi") in io.sends)
    # Casual idle fires _idle_then_compact: brief the live session, reseed a fresh one
    # from that brief, persist its new id. (old + reseed = two sessions built.)
    await _until(lambda: len(sessions) >= 2 and sessions[0].closed)

    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:0")
    assert db is not None
    assert db.status == OPEN  # casual stays OPEN — never marked DONE
    assert "-100:0" not in io.archived  # the :0 channel is never archived
    reseeded = db.sdk_session_id
    assert reseeded is not None and reseeded != "sess-hi"  # id swapped to the brief

    # The next message reopens the casual lane resumed from the small reseeded session.
    mgr._compaction_idle_seconds = 1000.0  # don't re-compact before we assert
    await mgr.dispatch(thread_key="-100:0", text="again", is_general=True)
    await _until(lambda: ("-100:0", "reply:again") in io.sends)
    assert sessions[-1].resume == reseeded
    await mgr.shutdown()


async def test_branch_forks_casual_into_a_tracked_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO(next_thread="-100:88")
    calls: list[dict[str, Any]] = []
    sessions: list[FakeSession] = []

    def factory(**kwargs: Any) -> SessionProto:
        calls.append(kwargs)
        sess = FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))
        sessions.append(sess)
        return sess

    mgr = _manager(session_factory, io, factory=factory, warrants=_no)

    # Establish a casual session with a persisted sdk_session_id.
    await mgr.dispatch(thread_key="-100:0", text="chat", is_general=True)
    await _until(lambda: ("-100:0", "reply:chat") in io.sends)

    new_key = await mgr.branch("-100:0", "Promoted chat")

    assert new_key == "-100:88"
    assert io.created == [("-100:0", "Promoted chat")]
    fork_call = calls[-1]  # the branched thread's session forks from the casual id
    assert fork_call["resume"] == "sess-chat"
    assert fork_call["fork_session"] is True

    # The forked session's new id is captured + persisted on its first turn; the casual
    # channel's own id is untouched (it keeps compacting independently).
    await mgr.dispatch(thread_key="-100:88", text="keep going")
    await _until(lambda: ("-100:88", "reply:keep going") in io.sends)
    async with session_factory() as session:
        new_db = await get_task(session, platform="telegram", thread_key="-100:88")
        casual = await get_task(session, platform="telegram", thread_key="-100:0")
    assert new_db is not None and new_db.sdk_session_id == "sess-keep going"
    assert casual is not None and casual.sdk_session_id == "sess-chat"
    await mgr.shutdown()


async def test_branch_empty_casual_starts_fresh(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO(next_thread="-100:88")
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> SessionProto:
        calls.append(kwargs)
        return FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))

    mgr = _manager(session_factory, io, factory=factory)

    # No casual turn has ever run, so there is no session to fork — the new thread
    # starts fresh (resume=None, fork_session=False) rather than forking nothing.
    new_key = await mgr.branch("-100:0", "Fresh topic")

    assert new_key == "-100:88"
    fork_call = calls[-1]
    assert fork_call["resume"] is None
    assert fork_call["fork_session"] is False
    await mgr.shutdown()


async def test_branch_prefers_live_session_id_over_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO(next_thread="-100:88")
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> SessionProto:
        calls.append(kwargs)
        return FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))

    mgr = _manager(session_factory, io, factory=factory, warrants=_no)

    await mgr.dispatch(thread_key="-100:0", text="chat", is_general=True)
    await _until(lambda: ("-100:0", "reply:chat") in io.sends)
    # Simulate a fresher live id than the persisted one (a mid-flight capture): branch
    # must fork from the live session, not the lagging DB row.
    mgr._tasks["-100:0"].session.session_id = "sess-live"

    await mgr.branch("-100:0", "Promoted")

    assert calls[-1]["resume"] == "sess-live"
    assert calls[-1]["fork_session"] is True
    await mgr.shutdown()


def _mem_factory(sessions: list[FakeSession]) -> Factory:
    """A session factory that tolerates the memory kwargs (system_prompt/cwd/tools)."""

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        sessions.append(sess)
        return sess

    return factory



# ---- calendar wiring (M5) ----------------------------------------------------


def _capture_factory(captured: dict[str, Any]) -> Factory:
    """A session factory that records the kwargs the engine assembled."""

    def factory(**kwargs: Any) -> SessionProto:
        captured.clear()
        captured.update(kwargs)
        return FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))

    return factory


def _calendar_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    enabled: bool = True,
) -> TaskManager:
    services = (
        (calendar_mcp.service("http://mcp-calendar:8003/mcp"),) if enabled else ()
    )
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        google_services=services,
        owner_tz="America/New_York",
    )


async def test_owner_calendar_session_wires_mcp_and_partitions_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _calendar_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["mcp_servers"] == {
        "calendar": {"type": "http", "url": "http://mcp-calendar:8003/mcp"}
    }
    allowed = captured["allowed_tools"]
    assert "mcp__calendar__list-events" in allowed  # reads pre-approved
    assert "mcp__calendar__create-event" not in allowed  # writes reach approval
    assert "Read" in allowed  # memory tools retained
    assert "WebSearch" in allowed  # web/meta tools added for the owner
    assert "ToolSearch" in allowed
    # deferred (delete) hard-blocked; reads/writes never land in disallowed
    assert "mcp__calendar__delete-event" in captured["disallowed_tools"]
    await mgr.shutdown()


async def test_owner_gmail_session_partitions_reads_writes_and_deletes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = TaskManager(
        session_factory=session_factory,
        io=FakeIO(),
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=_capture_factory(captured),
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        google_services=(gmail_mcp.chief_service("http://mcp-gmail:8004/mcp"),),
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["mcp_servers"] == {
        "gmail_chief": {"type": "http", "url": "http://mcp-gmail:8004/mcp"}
    }
    allowed = captured["allowed_tools"]
    assert "mcp__gmail_chief__gmail_search_messages" in allowed  # reads pre-approved
    assert "mcp__gmail_chief__gmail_send_message" not in allowed  # send → approval
    disallowed = captured["disallowed_tools"]
    # Permanent deletes hard-blocked; reads/writes never land in disallowed.
    assert "mcp__gmail_chief__gmail_delete_draft" in disallowed
    assert "mcp__gmail_chief__gmail_delete_label" in disallowed
    assert "mcp__gmail_chief__gmail_send_message" not in disallowed
    await mgr.shutdown()


# ---- skills wiring (M10) -----------------------------------------------------


def _skills_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    enabled: bool = True,
    plugin_path: str | None = "vendor/chief-skills",
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        front_desk_thread_key="-100:1",
        skills_enabled=enabled,
        skills_plugin_path=plugin_path,
        default_skills=("docx", "claude-api"),
    )


async def test_owner_session_wires_skills_plugin_and_filter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _skills_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["plugins"] == [
        {"type": "local", "path": "vendor/chief-skills"}
    ]
    assert captured["skills"] == ["docx", "claude-api"]
    # The owner prompt names the skills so chief reaches for them.
    assert "## Skills" in captured["system_prompt"]
    assert "docx" in captured["system_prompt"]
    await mgr.shutdown()


async def test_owner_session_no_skills_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _skills_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # Disabled → no plugin, no filter, no Skills block (SDK discovers nothing).
    assert "plugins" not in captured
    assert "skills" not in captured
    assert "## Skills" not in captured["system_prompt"]
    await mgr.shutdown()


async def test_guest_session_never_gets_skills(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Skills are owner-only: even with the framework enabled, a guest session carries
    # neither the plugin nor the filter (tier isolation, like the tool-surface split).
    captured: dict[str, Any] = {}
    mgr = _skills_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("555:0", tier="guest")

    assert "plugins" not in captured
    assert "skills" not in captured
    await mgr.shutdown()


async def test_guest_gets_no_calendar_or_web_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _calendar_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:9", tier="guest")

    assert "mcp_servers" not in captured  # no Google container for guests
    # Security keystone: a guest gets NONE of the owner's memory file tools and no
    # memory cwd — only its constructed receptionist tools (none wired in this manager).
    assert captured["allowed_tools"] == []
    assert captured.get("cwd") is None
    await mgr.shutdown()


def _guest_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    memory: FakeMemory,
    calendar: bool = True,
    front_desk: str | None = "-100:1",
) -> TaskManager:
    guest_cal = (
        calendar_mcp.guest_service("http://mcp-calendar:8003/mcp")
        if calendar
        else None
    )
    admin = GuestAdminService(session_factory=session_factory, platform="telegram")

    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="owner-model",
        guest_model="guest-model",
        classifier_model="claude-haiku-4-5",
        idle_archive_seconds=1000.0,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=memory,
        memory_dir="/tmp/mem",
        owner_name="Will",
        owner_tz="America/New_York",
        front_desk_thread_key=front_desk,
        guest_calendar_service=guest_cal,
        guest_admin_service=admin,
    )


async def test_guest_session_isolated_and_wires_only_guest_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _guest_manager(
        session_factory,
        FakeIO(),
        factory=_capture_factory(captured),
        memory=FakeMemory(),
    )

    await mgr.dispatch_guest(thread_key="555:0", text="hi", from_label="Alice")

    allowed = captured["allowed_tools"]
    # Leak fix: zero owner-memory/web tools, no memory cwd.
    for forbidden in (
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "WebSearch",
        "WebFetch",
        "ToolSearch",
    ):
        assert forbidden not in allowed
    assert captured.get("cwd") is None
    # Only the guest surface: relay + calendar free/busy (read), booking absent.
    assert "mcp__chief_guest__leave_message" in allowed
    assert "mcp__calendar__get-freebusy" in allowed
    assert "mcp__calendar__create-event" not in allowed  # write → approval card
    assert set(captured["mcp_servers"]) == {"chief_guest", "calendar"}
    assert "chief_guest_admin" not in captured["mcp_servers"]  # owner-only, never guest
    assert "chief_shell" not in captured["mcp_servers"]
    # The owner's built-in file/web/write tools are hard-denied at the SDK layer (not
    # merely absent from allowed_tools — the gate would otherwise classify them ALLOW).
    denied = captured["disallowed_tools"]
    for forbidden in ("Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch"):
        assert forbidden in denied
    assert "Bash" in denied  # the built-in shell stays denied too
    # Guest = guest model, never the owner's.
    assert captured["model"] == "guest-model"
    await mgr.shutdown()


async def test_owner_session_gets_guest_admin_tool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _guest_manager(
        session_factory,
        FakeIO(),
        factory=_capture_factory(captured),
        memory=FakeMemory(),
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert "mcp__chief_guest_admin__manage_guest" in captured["allowed_tools"]
    assert "chief_guest_admin" in captured["mcp_servers"]
    assert "Read" in captured["allowed_tools"]  # owner keeps memory tools
    assert captured["model"] == "owner-model"
    await mgr.shutdown()


async def test_guest_session_without_front_desk_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # With the gate wired, a guest approval with nowhere to route is a hard error
    # (belt-and-braces over the config validator) — never silently self-route to the DM.
    sentinel: Any = object()
    mgr = TaskManager(
        session_factory=session_factory,
        io=FakeIO(),
        owner_model="owner-model",
        guest_model="guest-model",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=_one(FakeSession(model="m")),
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        policy=sentinel,
        approvals=sentinel,
        audit=sentinel,
        front_desk_thread_key=None,
    )

    with pytest.raises(RuntimeError, match="front_desk"):
        await mgr._ensure_task("555:0", tier="guest")
    await mgr.shutdown()


async def test_calendar_disabled_owner_has_no_mcp_but_keeps_web_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _calendar_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert "mcp_servers" not in captured
    # No Google tools, but the owner still gets memory + web/meta tools.
    assert set(captured["allowed_tools"]) == set(MEMORY_TOOLS) | set(WEB_META_TOOLS)
    # The built-in shell stays disallowed even with no services wired.
    assert "Bash" in captured["disallowed_tools"]
    await mgr.shutdown()


# ---- shell + workspace wiring (M7) -------------------------------------------


def _shell_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    shell: bool = True,
    workspace: bool = True,
) -> TaskManager:
    service = (
        ShellService(
            workspace_dir="data/workspace",
            timeout_seconds=120.0,
            output_limit=64_000,
        )
        if shell
        else None
    )
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        shell_service=service,
        workspace_dir="/workspace" if workspace else None,
    )


async def test_owner_shell_and_workspace_wired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _shell_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # The in-process shell server is registered, but the bash tool stays OUT of the
    # allow-list so it routes through can_use_tool → approval (default-ask).
    assert "chief_shell" in captured["mcp_servers"]
    allowed = captured["allowed_tools"]
    assert "mcp__chief_shell__bash" not in allowed
    # Workspace write tools are pre-allowed (host-native: writes are unconfined).
    assert "Write" in allowed and "Edit" in allowed
    await mgr.shutdown()


async def test_workspace_disabled_owner_has_no_write_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _shell_manager(
        session_factory,
        FakeIO(),
        factory=_capture_factory(captured),
        shell=False,
        workspace=False,
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert "mcp_servers" not in captured  # no shell server
    allowed = captured["allowed_tools"]
    assert "Write" not in allowed and "Edit" not in allowed
    await mgr.shutdown()


async def test_guest_gets_no_shell_or_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _shell_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:9", tier="guest")

    assert "mcp_servers" not in captured  # no shell server for guests
    # Guests stay narrow: no memory tools, no Write/Edit, no shell (none wired here).
    assert captured["allowed_tools"] == []
    # The built-in shell is refused at the SDK layer for guests too.
    assert "Bash" in captured["disallowed_tools"]
    await mgr.shutdown()


async def test_builtin_shell_tools_disallowed_at_sdk_layer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _shell_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured)
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # Belt-and-braces with the gate's hard DENY: the SDK refuses the in-core shell
    # outright, so the model can't run a command where the Max token lives.
    disallowed = captured["disallowed_tools"]
    assert {"Bash", "BashOutput", "KillShell"} <= set(disallowed)
    await mgr.shutdown()


def _schedule_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    benign: bool = True,
    bash: bool = True,
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        schedule_service=(
            ScheduleService(session_factory=session_factory) if benign else None
        ),
        schedule_bash_service=(
            ScheduleBashService(session_factory=session_factory) if bash else None
        ),
    )


async def test_owner_schedule_tools_pre_approved_and_server_wired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _schedule_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured), bash=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # The benign schedule tools land on the allow-list (pre-approved, no card) and their
    # server is registered.
    allowed = captured["allowed_tools"]
    for name in ScheduleService(session_factory=session_factory).tool_names:
        assert name in allowed
    assert "chief_schedule" in captured["mcp_servers"]
    await mgr.shutdown()


async def test_owner_schedule_bash_server_wired_but_tools_gated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _schedule_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured), benign=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # The gated bash server is registered, but its tools stay OFF the allow-list so each
    # mint routes through can_use_tool → an approval card (like the shell tool).
    assert "chief_schedule_bash" in captured["mcp_servers"]
    allowed = captured["allowed_tools"]
    for name in ScheduleBashService(session_factory=session_factory).tool_names:
        assert name not in allowed
    await mgr.shutdown()


async def test_wake_runs_a_turn_in_the_target_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.wake(thread_key="-100:7", text="what time is it?")

    # A scheduled wakeup boots a normal turn: same run_turn → Final → io.send path.
    await _until(lambda: ("-100:7", "reply:what time is it?") in io.sends)
    assert sess.queries == ["what time is it?"]
    await mgr.shutdown()


def test_io_and_platform_properties_expose_wiring(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    mgr = _manager(session_factory, io, factory=_one(FakeSession(model="m")))

    # The scheduler is built against a stack's manager and reads these to target fires.
    assert mgr.io is io
    assert mgr.platform == "telegram"


# ---- budget enforcement (M9) ---------------------------------------------


async def test_clean_turn_records_spend(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m", cost=4.25)
    budget = FakeBudget()
    mgr = _manager(session_factory, io, factory=_one(sess), budget=budget)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    # The turn's SDK cost rolls into the monthly total; no rate-limit note.
    assert budget.recorded == [4.25]
    assert budget.rate_limited == 0
    await mgr.shutdown()


async def test_rate_limit_rejection_notes_budget(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m", cost=1.0, rate_limit="rejected")
    budget = FakeBudget()
    mgr = _manager(session_factory, io, factory=_one(sess), budget=budget)

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    # A hard rejection is treated like exhaustion — pause + ask.
    assert budget.recorded == [1.0]
    assert budget.rate_limited == 1
    await mgr.shutdown()


async def test_paused_skips_turn_and_acks_owner_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    budget = FakeBudget(mode_value="paused")
    mgr = _manager(
        session_factory, io, factory=_one(sess), budget=budget, owner_inbox="owner:0"
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await mgr.dispatch(thread_key="-100:5", text="hello again")
    await _until(lambda: ("owner:0", PAUSED_BUDGET_ACK) in io.sends)

    # Paused → the turn never runs (no spend) and the owner is reminded just once.
    assert sess.queries == []
    assert budget.recorded == []
    assert io.sends.count(("owner:0", PAUSED_BUDGET_ACK)) == 1
    assert ("-100:5", "reply:hi") not in io.sends
    await mgr.shutdown()


async def test_downgraded_owner_session_uses_downgrade_model(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="placeholder")
    budget = FakeBudget(mode_value="downgraded")
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        budget=budget,
        downgrade_model="claude-haiku-4-5",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    # A downgraded cycle builds the owner session on the cheaper model, and still runs.
    assert sess.model == "claude-haiku-4-5"
    await mgr.shutdown()


async def test_downgrade_live_sessions_switches_owner_models(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    budget = FakeBudget()  # normal — a live session exists before the owner taps
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        budget=budget,
        downgrade_model="claude-haiku-4-5",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    await mgr.downgrade_live_sessions()

    # The Downgrade tap flips every live owner session onto the budget model.
    assert sess.model == "claude-haiku-4-5"
    await mgr.shutdown()


async def test_downgrade_live_sessions_spares_guest_sessions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sessions: list[FakeSession] = []

    def factory(*, model: str, resume: str | None = None, **_: Any) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        sessions.append(sess)
        return sess

    mgr = TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="owner-model",
        guest_model="guest-model",
        classifier_model="claude-haiku-4-5",
        idle_archive_seconds=1000.0,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        budget=FakeBudget(),
        budget_downgrade_model="budget-model",
    )

    await mgr.dispatch(thread_key="-100:5", text="owner hi")
    await mgr.dispatch_guest(thread_key="555:0", text="guest hi", from_label="A")
    await _until(
        lambda: ("-100:5", "reply:owner hi") in io.sends
        and ("555:0", "reply:guest hi") in io.sends
    )
    owner_sess = next(s for s in sessions if s.model == "owner-model")
    guest_sess = next(s for s in sessions if s.model == "guest-model")

    await mgr.downgrade_live_sessions()

    # Only owner sessions follow the budget downgrade; a live guest keeps its model.
    assert owner_sess.model == "budget-model"
    assert guest_sess.model == "guest-model"
    await mgr.shutdown()


# ---- group chats (M11) -------------------------------------------------------


async def test_observe_buffers_then_engaged_owner_turn_sees_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    # warrants=_yes proves a GROUP turn stays flat (never spawns a topic) regardless.
    mgr = _manager(session_factory, io, factory=_one(sess), warrants=_yes)

    await mgr.observe(
        thread_key="-100:grp", text="launch is thursday", sender_name="Bob"
    )
    await mgr.observe(thread_key="-100:grp", text="cool", sender_name="Ada")
    await mgr.dispatch(
        thread_key="-100:grp", text="when's launch?", surface=Surface.GROUP
    )
    await _until(
        lambda: any(s[0] == "-100:grp" and "reply:" in s[1] for s in io.sends)
    )

    # Flat: no topic spawned despite warrants=yes — a group has no forum to branch into.
    assert io.created == []
    # The engaged turn carries the buffered ambient lines (attributed) + the new ask.
    turn = sess.queries[0]
    assert "Bob: launch is thursday" in turn
    assert "Ada: cool" in turn
    assert "when's launch?" in turn
    await mgr.shutdown()


async def test_group_buffer_drains_after_an_engaged_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.observe(thread_key="-100:grp", text="one", sender_name="Bob")
    await mgr.dispatch(thread_key="-100:grp", text="first", surface=Surface.GROUP)
    await _until(lambda: len(sess.queries) == 1)
    await mgr.dispatch(thread_key="-100:grp", text="second", surface=Surface.GROUP)
    await _until(lambda: len(sess.queries) == 2)

    # The buffered "one" rode the first turn only; the second turn isn't re-fed it.
    assert "Bob: one" in sess.queries[0]
    assert "Bob: one" not in sess.queries[1]
    await mgr.shutdown()


async def test_group_buffer_is_bounded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m")
    mgr = TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=_one(sess),
        stop_intent=_no,
        warrants_task=_no,
        group_context_max_messages=2,
    )

    for i in range(5):
        await mgr.observe(thread_key="-100:grp", text=f"m{i}", sender_name="Bob")
    await mgr.dispatch(thread_key="-100:grp", text="ask", surface=Surface.GROUP)
    await _until(lambda: len(sess.queries) == 1)

    turn = sess.queries[0]
    # Only the last 2 ambient messages survive the cap.
    assert "m3" in turn and "m4" in turn
    assert "m0" not in turn and "m1" not in turn and "m2" not in turn
    await mgr.shutdown()


def test_owner_group_approval_routes_to_owner_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mgr = _manager(
        session_factory,
        FakeIO(),
        factory=_one(FakeSession(model="m")),
        owner_inbox="42:0",
    )
    # In a group, an owner tool's approval card lands in the owner's private DM …
    assert (
        mgr._approval_route(tier="owner", thread_key="-100:grp", surface=Surface.GROUP)
        == "42:0"
    )
    # … but a DM/HOME owner turn still approves in-thread.
    assert (
        mgr._approval_route(tier="owner", thread_key="-100:5", surface=Surface.DM)
        == "-100:5"
    )


def test_owner_group_approval_without_owner_inbox_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Fail closed: an owner group card with no private route must NEVER fall back to the
    # public room — raise so a misconfigured wiring can't leak the card into the group.
    mgr = _manager(
        session_factory,
        FakeIO(),
        factory=_one(FakeSession(model="m")),
        owner_inbox=None,
    )
    with pytest.raises(RuntimeError, match="private route"):
        mgr._approval_route(tier="owner", thread_key="-100:grp", surface=Surface.GROUP)


def test_guest_group_approval_routes_to_front_desk_then_owner_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with_fd = _manager(
        session_factory,
        FakeIO(),
        factory=_one(FakeSession(model="m")),
        owner_inbox="42:0",
    )
    with_fd._front_desk_thread_key = "-100:1"
    assert (
        with_fd._approval_route(
            tier="guest", thread_key="-100:grp:guest", surface=Surface.GROUP
        )
        == "-100:1"
    )
    # No Front Desk in a group → card to the owner DM, never back into the group.
    no_fd = _manager(
        session_factory,
        FakeIO(),
        factory=_one(FakeSession(model="m")),
        owner_inbox="42:0",
    )
    assert (
        no_fd._approval_route(
            tier="guest", thread_key="-100:grp:guest", surface=Surface.GROUP
        )
        == "42:0"
    )


async def test_owner_group_session_gets_group_mode_note(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _calendar_manager(
        session_factory, FakeIO(), factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:grp", tier="owner", surface=Surface.GROUP)
    assert GROUP_MODE_NOTE in captured["system_prompt"]
    # A normal DM owner session does NOT carry the group note.
    await mgr._ensure_task("-100:5", tier="owner", surface=Surface.DM)
    assert GROUP_MODE_NOTE not in captured["system_prompt"]
    await mgr.shutdown()


async def test_guest_group_session_keyed_apart_and_sees_buffer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _guest_manager(
        session_factory,
        FakeIO(),
        factory=_capture_factory(captured),
        memory=FakeMemory(),
    )

    await mgr.observe(thread_key="-100:grp", text="hello room", sender_name="Cleo")
    await mgr.dispatch_guest(
        thread_key="-100:grp",
        text="who runs this?",
        from_label="Cleo",
        surface=Surface.GROUP,
    )

    # Guest gets its own key so it can never reuse the owner group session …
    assert "-100:grp:guest" in mgr._tasks
    assert "-100:grp" not in mgr._tasks
    assert captured["model"] == "guest-model"  # receptionist, never the owner model
    # … and it still reads the same shared ambient buffer.
    guest_sess = cast(FakeSession, mgr._tasks["-100:grp:guest"].session)
    await _until(lambda: bool(guest_sess.queries))
    assert "Cleo: hello room" in guest_sess.queries[0]
    assert "who runs this?" in guest_sess.queries[0]
    await mgr.shutdown()


# ---- Opus escalation (M11) ---------------------------------------------------


async def test_escalate_switches_live_session_and_persists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    reply = await mgr.escalate("-100:5")

    assert "Opus" in reply
    assert sess.model == "claude-opus-4-8"  # the live session flips immediately
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
        assert db is not None and db.model == "claude-opus-4-8"  # persisted
    await mgr.shutdown()


async def test_escalate_with_no_live_task_reopens_on_opus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sessions: list[FakeSession] = []
    mgr = _manager(session_factory, io, factory=_mem_factory(sessions))

    # /opus on a thread with no live session just persists the override …
    await mgr.escalate("-100:7")
    assert sessions == []
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:7")
        assert db is not None and db.model == "claude-opus-4-8"

    # … and the next turn opens the session on Opus (survives a restart).
    await mgr.dispatch(thread_key="-100:7", text="hi")
    await _until(lambda: ("-100:7", "reply:hi") in io.sends)
    assert sessions[-1].model == "claude-opus-4-8"
    await mgr.shutdown()


async def test_revert_clears_escalation_and_switches_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    await mgr.escalate("-100:5")
    assert sess.model == "claude-opus-4-8"

    reply = await mgr.revert("-100:5")

    assert "Sonnet" in reply
    assert sess.model == "claude-sonnet-4-6"  # back to the default owner model
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
        assert db is not None and db.model is None  # override cleared
    await mgr.shutdown()


async def test_escalate_overrides_budget_downgrade_with_warning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="placeholder")
    budget = FakeBudget(mode_value="downgraded")
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        budget=budget,
        downgrade_model="claude-haiku-4-5",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    assert sess.model == "claude-haiku-4-5"  # the cycle is downgraded

    reply = await mgr.escalate("-100:5")

    assert sess.model == "claude-opus-4-8"  # explicit escalation wins over downgrade
    assert "budget" in reply.lower()  # warns Opus burns the credit faster
    await mgr.shutdown()


async def test_revert_under_budget_downgrade_returns_to_downgrade_model(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="placeholder")
    budget = FakeBudget(mode_value="downgraded")
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        budget=budget,
        downgrade_model="claude-haiku-4-5",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    await mgr.escalate("-100:5")
    assert sess.model == "claude-opus-4-8"

    await mgr.revert("-100:5")

    # Reverting drops to the active budget downgrade model, not back to full Sonnet.
    assert sess.model == "claude-haiku-4-5"
    await mgr.shutdown()


async def test_auto_escalate_complex_turn_approved_switches_to_opus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    approvals = FakeApprovals(approve=True)
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        approvals=approvals,
        opus_auto_detect=True,
        is_complex=_yes,
    )

    await mgr.dispatch(thread_key="-100:5", text="design a sharded cache")
    await _until(lambda: ("-100:5", "reply:design a sharded cache") in io.sends)

    assert len(approvals.requests) == 1
    assert approvals.requests[0]["tool_name"] == OPUS_ESCALATION_KIND
    assert sess.model == "claude-opus-4-8"  # approved → the turn runs on Opus
    # The approved path posts the same confirmation as explicit /opus.
    assert any("Opus" in text for _key, text in io.sends)
    await mgr.shutdown()


async def test_escalated_thread_survives_a_budget_downgrade(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    budget = FakeBudget()  # normal — a live session exists before either action
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        budget=budget,
        downgrade_model="claude-haiku-4-5",
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    await mgr.escalate("-100:5")
    assert sess.model == "claude-opus-4-8"

    # A later Downgrade tap must NOT silently undo an explicit escalation: the pinned
    # thread stays on Opus (live and persisted agree), so it never re-asks or reopens
    # on a model that contradicts the live session.
    await mgr.downgrade_live_sessions()

    assert sess.model == "claude-opus-4-8"
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
        assert db is not None and db.model == "claude-opus-4-8"
    await mgr.shutdown()


async def test_auto_escalate_denied_stays_sonnet_and_suppresses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    approvals = FakeApprovals(approve=False)
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        approvals=approvals,
        opus_auto_detect=True,
        is_complex=_yes,
    )

    await mgr.dispatch(thread_key="-100:5", text="design one")
    await _until(lambda: ("-100:5", "reply:design one") in io.sends)
    assert sess.model == "claude-sonnet-4-6"  # denied → stays on the default
    assert len(approvals.requests) == 1

    # A second complex turn in the same task does not re-ask after a denial.
    await mgr.dispatch(thread_key="-100:5", text="design two")
    await _until(lambda: ("-100:5", "reply:design two") in io.sends)
    assert len(approvals.requests) == 1
    await mgr.shutdown()


async def test_auto_escalate_off_never_asks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    approvals = FakeApprovals(approve=True)
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        approvals=approvals,
        opus_auto_detect=False,  # opt-in flag off — only the command escalates
        is_complex=_yes,
    )

    await mgr.dispatch(thread_key="-100:5", text="design a sharded cache")
    await _until(lambda: ("-100:5", "reply:design a sharded cache") in io.sends)

    assert approvals.requests == []
    assert sess.model == "claude-sonnet-4-6"
    await mgr.shutdown()


async def test_auto_escalate_skips_when_already_on_opus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="claude-sonnet-4-6")
    approvals = FakeApprovals(approve=True)
    mgr = _manager(
        session_factory,
        io,
        factory=_one(sess),
        approvals=approvals,
        opus_auto_detect=True,
        is_complex=_yes,
    )

    await mgr.escalate("-100:5")  # explicit /opus pins the thread to Opus first
    await mgr.dispatch(thread_key="-100:5", text="design a sharded cache")
    await _until(lambda: ("-100:5", "reply:design a sharded cache") in io.sends)

    # Already on Opus → the per-turn classifier short-circuits, no card.
    assert approvals.requests == []
    assert sess.model == "claude-opus-4-8"
    await mgr.shutdown()


# ---- memory-write integration (M20) ------------------------------------------


async def test_memory_write_gate_allows_and_file_lands(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Turn-loop seam: owner Write to memory dir is ALLOWed; file lands on disk.

    The TaskManager assembles the gate (can_use_tool) and passes it to the
    session factory.  We capture those kwargs, invoke the can_use_tool callback
    directly against a User.md path inside the real memory_dir, and then write
    the file ourselves to assert it persists — end-to-end without a live Claude
    subprocess.
    """
    captured: dict[str, Any] = {}
    policy = PolicyStore(session_factory)
    await policy.seed(never=[], approved=[])
    audit = AuditLog(path=tmp_path / "audit.jsonl")

    mgr = TaskManager(
        session_factory=session_factory,
        io=FakeIO(),
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=_capture_factory(captured),
        stop_intent=_no,
        warrants_task=_no,
        memory=FakeMemory(),
        memory_dir=str(tmp_path),
        owner_name="William Chastain",
        policy=policy,
        approvals=cast(Any, FakeApprovals(approve=True)),
        audit=audit,
    )

    await mgr._ensure_task("-100:5", tier="owner")

    can_use_tool = captured.get("can_use_tool")
    assert can_use_tool is not None, "gate can_use_tool must be wired"

    # Simulate chief issuing a Write to User.md inside the memory directory.
    target = tmp_path / "User.md"
    payload = "## Preferences\nStays up late.\n\n## Facts\nBorn in Texas.\n"
    result = await can_use_tool(
        "Write",
        {"file_path": str(target), "content": payload},
        ToolPermissionContext(),
    )

    # Gate must ALLOW (not ASK, not DENY).
    assert isinstance(result, PermissionResultAllow), (
        f"Expected ALLOW for Write to memory_dir, got {result!r}"
    )

    # The file should land with the written content (the real SDK would write it;
    # we write it here to confirm the path is valid and persists across the test).
    target.write_text(payload, encoding="utf-8")
    assert target.exists()
    assert "## Preferences" in target.read_text(encoding="utf-8")
    assert "## Facts" in target.read_text(encoding="utf-8")
    await mgr.shutdown()


# ---- auto-commit memory after turn (issue #22) --------------------------------

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")


class FakeVersioner:
    """Versioner stub: records commit() calls and signals an asyncio.Event on each."""

    def __init__(self) -> None:
        self.commits: list[str] = []
        self.committed = asyncio.Event()

    async def init(self) -> None:
        return None

    async def commit(self, message: str) -> None:
        self.commits.append(message)
        self.committed.set()


async def _git_log(root: Path) -> list[str]:
    """Return git one-line commit subjects (newest first) for the repo at root."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "log", "--pretty=%s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return [line for line in out.decode().splitlines() if line.strip()]


def _versioned_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    versioner: Any,
    memory_dir: str,
) -> TaskManager:
    """Build a TaskManager wired with a versioner and memory_dir for #22 tests."""
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        versioner=versioner,
        memory=FakeMemory(),
        memory_dir=memory_dir,
        owner_name="Will",
    )


async def test_memory_auto_commit_fires_after_memory_touching_turn(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A turn that writes to the memory dir triggers versioner.commit() after settling.

    Uses FakeVersioner + GitVersioner to test both the hook timing and the actual
    git dirty-check: FakeVersioner signals when commit() is called; GitVersioner on a
    real repo confirms a commit lands only when files changed.
    """
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    versioner = FakeVersioner()

    written_file = mem_dir / "User.md"

    def _write_memory() -> None:
        written_file.write_text("profile update")

    # on_start fires inside run_turn — simulates chief writing a memory file mid-turn.
    sess = FakeSession(
        model="claude-sonnet-4-6",
        on_start=_write_memory,
    )
    io = FakeIO()
    mgr = _versioned_manager(
        session_factory,
        io,
        factory=_one(sess),
        versioner=versioner,
        memory_dir=str(mem_dir),
    )

    await mgr.dispatch(thread_key="-100:5", text="update my profile")
    # Wait for commit() to be called (fires after the turn's writes settle).
    await asyncio.wait_for(versioner.committed.wait(), timeout=2.0)
    await mgr.shutdown()

    assert len(versioner.commits) == 1
    assert versioner.commits[0] == "chief: memory auto-save"


@requires_git
async def test_memory_auto_commit_git_skips_when_no_memory_change(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A turn that leaves the memory dir clean produces no git commit.

    The GitVersioner's dirty-check (git status --porcelain) is the guard: a turn
    that does not write memory files results in no new commit in the git log.
    """
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    versioner = GitVersioner(
        mem_dir, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()

    # Seed one commit so the repo is non-empty; count should stay at 1 post-turn.
    (mem_dir / "Soul.md").write_text("# Soul")
    await versioner.commit("initial scaffold")

    fake_v = FakeVersioner()
    # Wrap: use FakeVersioner to detect call, then delegate to GitVersioner.

    class _DelegatingVersioner:
        async def init(self) -> None:
            return None

        async def commit(self, message: str) -> None:
            fake_v.commits.append(message)
            fake_v.committed.set()
            await versioner.commit(message)

    sess = FakeSession(model="claude-sonnet-4-6")  # no on_start → nothing written
    io = FakeIO()
    mgr = _versioned_manager(
        session_factory,
        io,
        factory=_one(sess),
        versioner=_DelegatingVersioner(),
        memory_dir=str(mem_dir),
    )

    await mgr.dispatch(thread_key="-100:5", text="what time is it?")
    # Wait for the commit() call (it fires, but git skips the empty commit).
    await asyncio.wait_for(fake_v.committed.wait(), timeout=2.0)
    await mgr.shutdown()

    # commit() was called once — the versioner always tries; git decides if dirty.
    assert len(fake_v.commits) == 1
    # git log still shows only the initial scaffold — no new commit landed.
    commits = await _git_log(mem_dir)
    assert len(commits) == 1, (
        f"no memory write → commit count must stay at 1, got {commits!r}"
    )


# ---- model routing (#79, part of #72) ----------------------------------------

_ROUTES = [
    ("writing", "copilot", "auto"),
    ("research", "copilot", "auto"),
    ("general", "copilot", "auto"),
    ("code", "openrouter", "deepseek/deepseek-v4-flash"),
    ("reasoning", "openrouter", "deepseek/deepseek-v4-flash"),
]

_OPENROUTER = ProviderConfig(base_url="https://openrouter.ai/api/v1", api_key="k")


def _recording_factory(created: list[dict[str, Any]]) -> Factory:
    """A factory that records the model + provider + resume each session is built on."""

    def factory(
        *,
        model: str,
        resume: str | None = None,
        provider: ProviderConfig | None = None,
        **_: Any,
    ) -> SessionProto:
        sess = FakeSession(model=model, resume=resume)
        created.append(
            {"session": sess, "model": model, "provider": provider, "resume": resume}
        )
        return sess

    return factory


async def _routed_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: Any,
    *,
    factory: Factory,
    classify_category: Callable[..., Any],
    surface_defaults: dict[str, str] | None = None,
    openrouter_provider: ProviderConfig | None = _OPENROUTER,
    idle: float = 1000.0,
    compaction: float = 1000.0,
) -> TaskManager:
    routing = RoutingStore(session_factory)
    await routing.seed(_ROUTES)
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        idle_archive_seconds=idle,
        compaction_idle_seconds=compaction,
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        routing=routing,
        classify_category=classify_category,
        openrouter_provider=openrouter_provider,
        routing_surface_defaults=surface_defaults or {},
    )


async def test_owner_message_auto_routed_to_category_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1: message types are auto-classified and served by the category's target —
    served-model assert for the openrouter category, provider(None) assert for auto."""
    io = FakeIO()
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "code" if "bug" in text else "writing"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )

    # A "code" message → the openrouter DeepSeek target: assert the served model.
    await mgr.dispatch(thread_key="-100:5", text="fix this bug")
    await _until(lambda: ("-100:5", "reply:fix this bug") in io.sends)
    code = created[-1]
    assert code["model"] == "deepseek/deepseek-v4-flash"
    assert code["provider"] is _OPENROUTER  # BYOK provider for openrouter
    assert code["session"].last_served_model == "deepseek/deepseek-v4-flash"

    # A "writing" message → copilot auto: the request stays on Copilot quota (no BYOK
    # provider), and auto is the served model.
    await mgr.dispatch(thread_key="-100:6", text="draft a poem")
    await _until(lambda: ("-100:6", "reply:draft a poem") in io.sends)
    writing = created[-1]
    assert writing["model"] == "auto"
    assert writing["provider"] is None  # plain Copilot quota (the `auto` target)
    await mgr.shutdown()


async def test_route_command_overrides_task_and_respawns_on_new_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2: /route overrides a task — persisted and, since a provider can't change on a
    live session, the session is respawned (resume-preserving) onto the new target."""
    io = FakeIO()
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "writing"  # would auto-route to copilot auto

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )

    await mgr.dispatch(thread_key="-100:5", text="hello")
    await _until(lambda: ("-100:5", "reply:hello") in io.sends)
    assert created[-1]["model"] == "auto"  # auto-classified writing → copilot auto

    reply = await mgr.route("-100:5", "code")

    assert "code" in reply
    respawned = created[-1]
    assert respawned["model"] == "deepseek/deepseek-v4-flash"  # switched target class
    assert respawned["provider"] is _OPENROUTER
    assert respawned["resume"] == "sess-hello"  # context carried via resume
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
        assert db is not None and db.route_category == "code"  # persisted per task
    await mgr.shutdown()


async def test_route_rejects_unknown_category(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "general"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )
    reply = await mgr.route("-100:5", "banana")
    assert "Unknown category" in reply  # the table is the source of truth
    await mgr.shutdown()


async def test_surface_default_pins_category_without_classifying(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2: config defaults apply per surface — a configured surface default pins the
    category and skips the classifier entirely."""
    io = FakeIO()
    created: list[dict[str, Any]] = []
    classify_calls: list[str] = []

    async def fake_classify(text: str, **_: Any) -> str:
        classify_calls.append(text)
        return "code"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
        surface_defaults={"group": "general"},
    )

    await mgr.dispatch(thread_key="-100:grp", text="anything", surface=Surface.GROUP)
    await _until(lambda: ("-100:grp", "reply:anything") in io.sends)

    # general → copilot auto (the surface default), not the classifier's "code".
    assert created[-1]["model"] == "auto"
    assert created[-1]["provider"] is None
    assert classify_calls == []  # the classifier was never consulted
    await mgr.shutdown()


async def test_classifier_runs_on_fixed_cheap_model_never_a_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3: the classifier runs on the fixed cheap classifier model, its label space is
    the routing table's categories, and it is never handed a routing target model."""
    io = FakeIO()
    created: list[dict[str, Any]] = []
    seen: dict[str, Any] = {}

    async def fake_classify(
        text: str, *, model: str, categories: Any, default: str
    ) -> str:
        seen["model"] = model
        seen["categories"] = set(categories)
        return "code"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )

    await mgr.dispatch(thread_key="-100:5", text="fix the bug")
    await _until(lambda: ("-100:5", "reply:fix the bug") in io.sends)

    assert seen["model"] == "claude-haiku-4-5"  # the fixed cheap classifier target
    assert seen["model"] != "deepseek/deepseek-v4-flash"  # never a routing target
    assert seen["categories"] == {
        "writing",
        "research",
        "general",
        "code",
        "reasoning",
    }
    await mgr.shutdown()


# ---- reseed / branch bypass routing (#92, part of #72) ------------------------


async def test_casual_reseed_rebuilds_on_the_routed_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#92 AC1: a reseed of a routed casual task rebuilds on the category's
    {model, provider} — not the owner model / plain Copilot quota."""
    io = FakeIO()
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "code" if "bug" in text else "writing"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
        compaction=0.02,
    )

    await mgr.dispatch(thread_key="-100:0", text="fix this bug", is_general=True)
    await _until(lambda: ("-100:0", "reply:fix this bug") in io.sends)
    routed = created[-1]
    assert routed["model"] == "deepseek/deepseek-v4-flash"
    assert routed["provider"] is _OPENROUTER

    # Casual idle fires _idle_then_compact → _summarize_and_reseed: the fresh reseeded
    # session must land on the same routed target, not the owner model / Copilot quota.
    await _until(lambda: len(created) >= 2 and created[0]["session"].closed)
    reseeded = created[-1]
    assert reseeded["model"] == "deepseek/deepseek-v4-flash"
    assert reseeded["provider"] is _OPENROUTER
    await mgr.shutdown()


async def test_branch_of_routed_casual_produces_task_on_routed_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#92 AC2: branch of a routed casual context produces a task on the category's
    {model, provider} — not the owner model / plain Copilot quota. The casual thread
    was only auto-classified (no explicit /route), so this also proves branch carries
    forward the live resolved target rather than an unpersisted route_category."""
    io = FakeIO(next_thread="-100:88")
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "code" if "bug" in text else "writing"

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )

    await mgr.dispatch(thread_key="-100:0", text="fix this bug", is_general=True)
    await _until(lambda: ("-100:0", "reply:fix this bug") in io.sends)
    assert created[-1]["model"] == "deepseek/deepseek-v4-flash"  # sanity: routed

    new_key = await mgr.branch("-100:0", "Promoted bug thread")

    assert new_key == "-100:88"
    branched = created[-1]
    assert branched["model"] == "deepseek/deepseek-v4-flash"
    assert branched["provider"] is _OPENROUTER
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:88")
    assert db is not None and db.route_category is None  # no persisted override used
    await mgr.shutdown()


async def test_branch_of_escalated_casual_keeps_opus_over_routing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#92: the precedence is Opus escalation > budget downgrade > routing > owner
    model, and neither reseed nor branch may reorder it. An escalated casual thread
    branches onto Opus even though its category routes to openrouter."""
    io = FakeIO(next_thread="-100:88")
    created: list[dict[str, Any]] = []

    async def fake_classify(text: str, **_: Any) -> str:
        return "code"  # would route to the openrouter DeepSeek target

    mgr = await _routed_manager(
        session_factory,
        io,
        factory=_recording_factory(created),
        classify_category=fake_classify,
    )

    await mgr.dispatch(thread_key="-100:0", text="fix this bug", is_general=True)
    await _until(lambda: ("-100:0", "reply:fix this bug") in io.sends)
    assert created[-1]["model"] == "deepseek/deepseek-v4-flash"  # sanity: routed

    await mgr.escalate("-100:0")  # explicit /opus wins outright (M11)

    new_key = await mgr.branch("-100:0", "Escalated thread")

    assert new_key == "-100:88"
    branched = created[-1]
    assert branched["model"] == "claude-opus-4-8"  # Opus wins over routing
    assert branched["provider"] is None  # plain Copilot quota, not BYOK
    await mgr.shutdown()
