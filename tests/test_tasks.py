"""TaskManager: hybrid grace, steering, interrupt, semaphore, idle, recovery, spawn."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import Attachment
from chief.core.session import Final, Milestone, TurnEvent
from chief.core.tasks import (
    MEMORY_TOOLS,
    WEB_META_TOOLS,
    WORKING_ACK,
    SessionProto,
    TaskManager,
)
from chief.memory.store import Fact, FactDraft
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
from chief.tools.shell import ShellService

Factory = Callable[..., SessionProto]


class FakeMemory:
    """In-memory MemoryStore: records writes, returns a static index for distill."""

    def __init__(self) -> None:
        self.written: list[Fact] = []

    def index(self) -> str:
        return "- facts/owner/x.md — X"

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return "# Will"

    def list_facts(self, namespace: str) -> list[Fact]:
        return [f for f in self.written if f.namespace == namespace]

    async def write_fact(
        self,
        *,
        namespace: str,
        slug: str,
        title: str,
        body: str,
        provenance: str,
        trust: str,
        expires: str | None = None,
    ) -> Fact:
        fact = Fact(
            slug=slug,
            title=title,
            body=body,
            namespace=namespace,
            provenance=provenance,
            trust=trust,
            expires=expires,
            created="2026-06-03T00:00:00+00:00",
        )
        self.written.append(fact)
        return fact

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
    ) -> None:
        self.model = model
        self.resume = resume
        self.session_id = resume
        self.queries: list[str] = []
        self.attachments_seen: list[tuple[Attachment, ...]] = []
        self.interrupted = False
        self.closed = False
        self._gate = gate
        self._on_start = on_start
        self._milestones = milestones or []
        self._after_gate = after_gate or []

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        self.queries.append(text)
        self.attachments_seen.append(tuple(attachments))
        if self._on_start is not None:
            self._on_start()
        for milestone in self._milestones:
            yield milestone
        if self._gate is not None:
            await self._gate.wait()
        self.session_id = f"sess-{text}"
        for milestone in self._after_gate:  # yielded post-gate (after a cancel flips)
            yield milestone
        yield Final(text=f"reply:{text}")

    async def interrupt(self) -> None:
        self.interrupted = True
        if self._gate is not None:
            self._gate.set()

    async def aclose(self) -> None:
        self.closed = True


class FakeIO:
    def __init__(self, next_thread: str = "-100:99") -> None:
        self.sends: list[tuple[str, str]] = []
        self.created: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self._next = next_thread

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

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
    io: FakeIO,
    *,
    factory: Factory,
    stop: Callable[..., Any] = _no,
    warrants: Callable[..., Any] = _no,
    concurrency: int = 3,
    grace: float = 5.0,
    idle: float = 1000.0,
    compaction: float = 1000.0,
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        concurrency=concurrency,
        grace_seconds=grace,
        idle_archive_seconds=idle,
        compaction_idle_seconds=compaction,
        session_factory_sdk=factory,
        stop_intent=stop,
        warrants_task=warrants,
    )


async def _until(pred: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


async def test_fast_turn_replies_inline_without_ack(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    sess = FakeSession(model="m", milestones=[Milestone(text="using Bash")])
    mgr = _manager(session_factory, io, factory=_one(sess))

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)

    assert ("-100:5", "· using Bash") in io.sends  # milestone posted
    assert ("-100:5", WORKING_ACK) not in io.sends  # fast → no working ack
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


async def test_slow_turn_acks_then_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    gate = asyncio.Event()
    sess = FakeSession(model="m", gate=gate)
    mgr = _manager(session_factory, io, factory=_one(sess), grace=0.01)

    await mgr.dispatch(thread_key="-100:5", text="slow")
    await _until(lambda: ("-100:5", WORKING_ACK) in io.sends)
    assert ("-100:5", "reply:slow") not in io.sends

    gate.set()
    await _until(lambda: ("-100:5", "reply:slow") in io.sends)
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


def _mem_manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: FakeIO,
    *,
    factory: Factory,
    memory: FakeMemory,
    distill: Callable[..., Any],
    distill_idle: float = 0.02,
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        idle_archive_seconds=1000.0,  # keep archive out of the way of distill
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=memory,
        memory_dir="/tmp/mem",
        owner_name="Will",
        distill_idle_seconds=distill_idle,
        distill_model="claude-sonnet-4-6",
        distill=distill,
    )


async def test_distill_saves_facts_notifies_and_stays_open(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    memory = FakeMemory()
    captured: dict[str, Any] = {}

    async def distill(
        transcript: list[tuple[str, str]], index: str, *, model: str
    ) -> list[FactDraft]:
        captured["transcript"] = transcript
        captured["index"] = index
        return [FactDraft(slug="mornings", title="Prefers mornings", body="b")]

    mgr = _mem_manager(
        session_factory, io, factory=_mem_factory([]), memory=memory, distill=distill
    )

    await mgr.dispatch(thread_key="-100:5", text="i prefer mornings")
    await _until(lambda: ("-100:5", "reply:i prefer mornings") in io.sends)
    await _until(lambda: any("📝 Saved to memory" in t for _, t in io.sends))

    # The whole turn (owner + chief) is handed to the distiller, with the live index.
    assert captured["transcript"] == [
        ("owner", "i prefer mornings"),
        ("chief", "reply:i prefer mornings"),
    ]
    assert captured["index"] == "- facts/owner/x.md — X"
    # The draft is persisted to the owner namespace as an inferred fact.
    assert len(memory.written) == 1
    fact = memory.written[0]
    assert (fact.slug, fact.namespace, fact.provenance) == (
        "mornings",
        "owner",
        "inferred",
    )
    # Distilling does not end the task — it stays OPEN (and unarchived).
    assert "-100:5" not in io.archived
    async with session_factory() as session:
        db = await get_task(session, platform="telegram", thread_key="-100:5")
    assert db is not None and db.status == OPEN
    await mgr.shutdown()


async def test_distill_no_drafts_writes_nothing_and_is_silent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    memory = FakeMemory()
    calls = {"n": 0}

    async def distill(*args: Any, **kwargs: Any) -> list[FactDraft]:
        calls["n"] += 1
        return []

    mgr = _mem_manager(
        session_factory, io, factory=_mem_factory([]), memory=memory, distill=distill
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: calls["n"] == 1)  # distiller ran on the quiet window

    assert memory.written == []
    assert not any("📝" in t for _, t in io.sends)  # nothing to announce
    await mgr.shutdown()


async def test_new_message_cancels_pending_distill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    memory = FakeMemory()

    async def distill(*args: Any, **kwargs: Any) -> list[FactDraft]:
        return [FactDraft(slug="s", title="T", body="b")]

    # A long distill window so the timer is still pending when the next message lands.
    mgr = _mem_manager(
        session_factory,
        io,
        factory=_mem_factory([]),
        memory=memory,
        distill=distill,
        distill_idle=50.0,
    )

    await mgr.dispatch(thread_key="-100:5", text="first")
    await _until(lambda: ("-100:5", "reply:first") in io.sends)
    task = mgr._tasks["-100:5"]
    await _until(lambda: task.distill_handle is not None)  # armed after the clean turn

    await mgr.dispatch(thread_key="-100:5", text="second")
    await _until(lambda: ("-100:5", "reply:second") in io.sends)

    # The second turn accumulates onto the same transcript (no distill fired between).
    assert task.transcript == [
        ("owner", "first"),
        ("chief", "reply:first"),
        ("owner", "second"),
        ("chief", "reply:second"),
    ]
    assert memory.written == []  # the pending distill was cancelled, none fired
    await mgr.shutdown()


async def test_distill_cancelled_mid_flush_keeps_transcript(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    memory = FakeMemory()
    entered, release = asyncio.Event(), asyncio.Event()

    async def distill(*args: Any, **kwargs: Any) -> list[FactDraft]:
        entered.set()
        await release.wait()  # park inside the flush, holding the cancel window open
        return [FactDraft(slug="s", title="T", body="b")]

    mgr = _mem_manager(
        session_factory, io, factory=_mem_factory([]), memory=memory, distill=distill
    )

    await mgr.dispatch(thread_key="-100:5", text="hi")
    await _until(lambda: ("-100:5", "reply:hi") in io.sends)
    task = mgr._tasks["-100:5"]
    await entered.wait()  # distiller is now parked mid-flush

    # The cancel-during-flush race: a new turn cancels the timer while distilling.
    mgr._cancel_distill(task)
    await asyncio.sleep(0)  # let the CancelledError propagate into the parked task

    # The transcript survives for the next pass instead of being silently lost.
    assert task.transcript == [("owner", "hi"), ("chief", "reply:hi")]
    assert memory.written == []
    await mgr.shutdown()


async def test_distill_failure_is_logged_with_thread_key(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    io = FakeIO()
    memory = FakeMemory()

    async def distill(*args: Any, **kwargs: Any) -> list[FactDraft]:
        raise RuntimeError("boom")

    mgr = _mem_manager(
        session_factory, io, factory=_mem_factory([]), memory=memory, distill=distill
    )

    with caplog.at_level(logging.ERROR, logger="chief.core.tasks"):
        await mgr.dispatch(thread_key="-100:5", text="hi")
        await _until(
            lambda: any(r.msg == "distillation failed" for r in caplog.records)
        )

    rec = next(r for r in caplog.records if r.msg == "distillation failed")
    assert rec.__dict__.get("thread_key") == "-100:5"  # context, not a bare traceback
    assert memory.written == []
    await mgr.shutdown()


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
        google_services=(gmail_mcp.service("http://mcp-gmail:8004/mcp"),),
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["mcp_servers"] == {
        "gmail": {"type": "http", "url": "http://mcp-gmail:8004/mcp"}
    }
    allowed = captured["allowed_tools"]
    assert "mcp__gmail__gmail_search_messages" in allowed  # reads pre-approved
    assert "mcp__gmail__gmail_send_message" not in allowed  # send reaches approval
    disallowed = captured["disallowed_tools"]
    # Permanent deletes hard-blocked; reads/writes never land in disallowed.
    assert "mcp__gmail__gmail_delete_draft" in disallowed
    assert "mcp__gmail__gmail_delete_label" in disallowed
    assert "mcp__gmail__gmail_send_message" not in disallowed
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
    distill: Callable[..., Any] | None = None,
    distill_idle: float = 1000.0,
) -> TaskManager:
    guest_cal = (
        calendar_mcp.guest_service("http://mcp-calendar:8003/mcp")
        if calendar
        else None
    )
    admin = GuestAdminService(session_factory=session_factory, platform="telegram")

    async def _distill_none(*_a: Any, **_k: Any) -> list[FactDraft]:
        return []

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
        distill=distill or _distill_none,
        distill_idle_seconds=distill_idle,
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


async def test_guest_turn_is_not_distilled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    memory = FakeMemory()

    async def _distill_yes(*_a: Any, **_k: Any) -> list[FactDraft]:
        return [FactDraft(slug="x", title="X", body="b")]

    mgr = _guest_manager(
        session_factory,
        io,
        factory=_mem_factory([]),
        memory=memory,
        distill=_distill_yes,
        distill_idle=0.02,
    )

    await mgr.dispatch_guest(thread_key="555:0", text="hi", from_label="Alice")
    await _until(lambda: ("555:0", "reply:hi") in io.sends)
    await asyncio.sleep(0.05)  # past the distill window

    assert memory.written == []  # a guest's low-trust chatter never enters owner memory
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
            host="sandbox", port=8765, timeout_seconds=120.0, output_limit=64_000
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
    # Workspace write tools are pre-allowed (the gate confines them to /workspace).
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
