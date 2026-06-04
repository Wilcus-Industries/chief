"""TaskManager: hybrid grace, steering, interrupt, semaphore, idle, recovery, spawn."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.core.session import Final, Milestone, TurnEvent
from chief.core.tasks import WORKING_ACK, SessionProto, TaskManager
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

Factory = Callable[..., SessionProto]


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
        self.interrupted = False
        self.closed = False
        self._gate = gate
        self._on_start = on_start
        self._milestones = milestones or []
        self._after_gate = after_gate or []

    async def run_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        self.queries.append(text)
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
    def factory(*, model: str, resume: str | None = None) -> SessionProto:
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
) -> TaskManager:
    return TaskManager(
        session_factory=session_factory,
        io=io,
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        concurrency=concurrency,
        grace_seconds=grace,
        idle_archive_seconds=idle,
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

    def factory(*, model: str, resume: str | None = None) -> SessionProto:
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

    def factory(*, model: str, resume: str | None = None) -> SessionProto:
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

    def factory(*, model: str, resume: str | None = None) -> SessionProto:
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
        async def run_turn(self, text: str) -> AsyncIterator[TurnEvent]:
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

    def factory(*, model: str, resume: str | None = None) -> SessionProto:
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
