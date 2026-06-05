"""Scheduler engine (chief.core.scheduler): dispatch, advance, quiet hours, catch-up.

Fakes stand in for the platform IO, the wakeup entry, and the sandbox shell so the
engine is exercised in isolation. Owner tz is America/New_York; ``EPOCH`` (12:00 EDT) is
deliberately outside any quiet window so non-quiet tests fire cleanly.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.core.scheduler import Scheduler
from chief.persistence.schedules import (
    ACTION_BASH,
    ACTION_MESSAGE,
    ACTION_WAKEUP,
    KIND_ONCE,
    KIND_RECURRING,
    create_schedule,
    get_schedule,
)
from chief.tools.shell import ShellService

NY = ZoneInfo("America/New_York")
#: 12:00 EDT — midday, outside the 22:00→07:00 quiet window the quiet tests use.
EPOCH = datetime(2026, 6, 4, 16, 0, tzinfo=UTC)


def _as_utc(value: datetime | None) -> datetime:
    """Re-stamp UTC on a value sqlite handed back naive (asserts it is present)."""
    assert value is not None
    return value.replace(tzinfo=UTC)


class FakeIO:
    """Records the engine's outbound sends and file deliveries."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, bytes]] = []

    async def send(self, thread_key: str, text: str) -> None:
        self.sent.append((thread_key, text))

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        self.files.append((thread_key, filename, data))


class FakeWaker:
    """Records wakeup invocations (the real one boots a gated agent turn)."""

    def __init__(self) -> None:
        self.woke: list[tuple[str, str]] = []

    async def wake(self, *, thread_key: str, text: str) -> None:
        self.woke.append((thread_key, text))


async def _unused_run_command(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise AssertionError("run_command should not be called in this test")


def _make(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    io: FakeIO | None = None,
    waker: FakeWaker | None = None,
    shell_service: ShellService | None = None,
    run_command: Callable[..., Awaitable[dict[str, Any]]] = _unused_run_command,
    now: datetime = EPOCH,
    quiet_start: str | None = None,
    quiet_end: str = "07:00",
    heartbeat_url: str | None = None,
    http: Any = None,
) -> Scheduler:
    return Scheduler(
        session_factory=session_factory,
        io=io or FakeIO(),
        waker=waker or FakeWaker(),
        shell_service=shell_service,
        run_command=run_command,
        primary_thread_key="inbox",
        owner_tz="America/New_York",
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        tick_seconds=30.0,
        message_limit=4096,
        heartbeat_url=heartbeat_url,
        http=http,
        now=lambda: now,
    )


async def test_message_fire_sends_and_disables_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="stretch",
            action_type=ACTION_MESSAGE,
            next_run=EPOCH,
        )
    io = FakeIO()
    await _make(session_factory, io=io).tick()

    assert io.sent == [("inbox", "stretch")]  # None thread_key ⇒ primary inbox
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False
        assert fresh.last_run is not None


async def test_wakeup_fire_wakes_target_and_advances_recurring(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_RECURRING,
            spec="* * * * *",
            action="what time?",
            action_type=ACTION_WAKEUP,
            next_run=EPOCH,
            thread_key="-100:7",
        )
    waker = FakeWaker()
    await _make(session_factory, waker=waker).tick()

    assert waker.woke == [("-100:7", "what time?")]  # explicit thread_key honored
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is True
        # "* * * * *" from 12:00:00 EDT → 12:01 EDT = 16:01 UTC.
        assert _as_utc(fresh.next_run) == datetime(2026, 6, 4, 16, 1, tzinfo=UTC)
        assert _as_utc(fresh.last_run) == EPOCH


async def test_bash_fire_runs_in_sandbox_and_sends_output(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def fake_rc(
        host: str, port: int, session_id: str, command: str, *, read_timeout: float
    ) -> dict[str, Any]:
        calls.append((host, port, session_id, command, read_timeout))
        return {"stdout": "disk ok\n", "exit_code": 0}

    shell = ShellService(
        host="sandbox", port=8765, timeout_seconds=120.0, output_limit=64_000
    )
    io = FakeIO()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_RECURRING,
            spec="0 2 * * *",
            action="df -h",
            action_type=ACTION_BASH,
            next_run=EPOCH,
        )
    await _make(session_factory, io=io, shell_service=shell, run_command=fake_rc).tick()

    assert len(calls) == 1
    host, port, session_id, command, read_timeout = calls[0]
    assert (host, port, command) == ("sandbox", 8765, "df -h")
    assert session_id == f"schedule:{sched.id}"
    assert read_timeout == 150.0  # timeout_seconds (120) + client grace (30)
    assert len(io.sent) == 1
    target, body = io.sent[0]
    assert target == "inbox"
    assert "df -h" in body and "disk ok" in body


async def test_bash_long_output_sent_as_file(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    big = "x" * (4096 * 5)  # over the file threshold

    async def fake_rc(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"stdout": big, "exit_code": 0}

    shell = ShellService(host="h", port=1, timeout_seconds=1.0, output_limit=10)
    io = FakeIO()
    async with session_factory() as s:
        await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="dump",
            action_type=ACTION_BASH,
            next_run=EPOCH,
        )
    await _make(session_factory, io=io, shell_service=shell, run_command=fake_rc).tick()

    assert io.sent == []
    assert len(io.files) == 1
    assert io.files[0][0] == "inbox"


async def test_bash_without_shell_skips_run_but_advances(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="df -h",
            action_type=ACTION_BASH,
            next_run=EPOCH,
        )
    # shell_service None + the default run_command would assert if called.
    await _make(session_factory, io=io, shell_service=None).tick()

    assert io.sent == []
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False  # advanced (once → disabled) so it can't hot-loop


async def test_quiet_hours_defers_non_urgent_without_firing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 5, 3, 0, tzinfo=UTC)  # 23:00 EDT Jun 4, inside the window
    io = FakeIO()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="hey",
            action_type=ACTION_MESSAGE,
            next_run=now,
        )
    await _make(
        session_factory, io=io, now=now, quiet_start="22:00", quiet_end="07:00"
    ).tick()

    assert io.sent == []  # deferred, not fired
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is True
        assert fresh.last_run is None
        # Released at the next 07:00 EDT = 11:00 UTC Jun 5.
        assert _as_utc(fresh.next_run) == datetime(2026, 6, 5, 11, 0, tzinfo=UTC)


async def test_quiet_hours_urgent_fires_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 5, 3, 0, tzinfo=UTC)
    io = FakeIO()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="wake up!",
            action_type=ACTION_MESSAGE,
            next_run=now,
            urgent=True,
        )
    await _make(
        session_factory, io=io, now=now, quiet_start="22:00", quiet_end="07:00"
    ).tick()

    assert io.sent == [("inbox", "wake up!")]
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False


async def test_restart_catch_up_fires_once_and_advances_forward(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    overdue = EPOCH - timedelta(hours=5)  # 07:00 EDT — missed during downtime
    waker = FakeWaker()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_RECURRING,
            spec="0 * * * *",  # top of every hour
            action="hourly",
            action_type=ACTION_WAKEUP,
            next_run=overdue,
        )
    await _make(session_factory, waker=waker, now=EPOCH).tick()

    # Fired once despite five missed hours (no thundering herd).
    assert waker.woke == [("inbox", "hourly")]
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        # Advanced to the next top-of-hour AFTER now (13:00 EDT = 17:00 UTC), not 08:00.
        assert _as_utc(fresh.next_run) == datetime(2026, 6, 4, 17, 0, tzinfo=UTC)


async def test_bash_run_failure_notifies_owner_and_advances(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("sandbox down")

    shell = ShellService(host="h", port=1, timeout_seconds=1.0, output_limit=10)
    io = FakeIO()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="df -h",
            action_type=ACTION_BASH,
            next_run=EPOCH,
        )
    await _make(session_factory, io=io, shell_service=shell, run_command=boom).tick()

    assert len(io.sent) == 1
    target, body = io.sent[0]
    assert target == "inbox"
    assert "failed" in body and "sandbox down" in body
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False  # advanced (once → disabled) despite the failure


async def test_recurring_bad_cron_fires_once_then_disables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    waker = FakeWaker()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_RECURRING,
            spec="not a cron",
            action="hi",
            action_type=ACTION_WAKEUP,
            next_run=EPOCH,
        )
    await _make(session_factory, waker=waker).tick()

    assert waker.woke == [("inbox", "hi")]  # fired this tick
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False  # unparseable spec → disabled, no hot-loop


async def test_unknown_action_type_does_not_fire_but_advances(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io = FakeIO()
    waker = FakeWaker()
    async with session_factory() as s:
        sched = await create_schedule(
            s,
            kind=KIND_ONCE,
            spec="x",
            action="?",
            action_type="monitor",  # not a fire path this milestone handles (M9b)
            next_run=EPOCH,
        )
    await _make(session_factory, io=io, waker=waker).tick()

    assert io.sent == []
    assert waker.woke == []
    async with session_factory() as s:
        fresh = await get_schedule(s, sched.id)
        assert fresh is not None
        assert fresh.enabled is False  # still advanced (once → disabled), no hot-loop


async def test_heartbeat_once_pings_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class FakeHttp:
        def __init__(self) -> None:
            self.gets: list[str] = []

        async def get(self, url: str, *, timeout: float) -> object:
            self.gets.append(url)
            return None

    http = FakeHttp()
    sched = _make(session_factory, heartbeat_url="http://hc/ping", http=http)
    await sched.heartbeat_once()
    assert http.gets == ["http://hc/ping"]


async def test_heartbeat_once_noop_without_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No url and no client → must be a harmless no-op (not raise).
    await _make(session_factory).heartbeat_once()


async def test_heartbeat_once_swallows_ping_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class BoomHttp:
        async def get(self, url: str, *, timeout: float) -> object:
            raise ConnectionError("monitor unreachable")

    sched = _make(session_factory, heartbeat_url="http://hc/ping", http=BoomHttp())
    await sched.heartbeat_once()  # a failed ping is logged, never raised
