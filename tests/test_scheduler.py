"""Scheduler engine (chief.core.scheduler): dispatch, advance, quiet hours, catch-up.

Fakes stand in for the platform IO, the wakeup entry, and the sandbox shell so the
engine is exercised in isolation. Owner tz is America/New_York; ``EPOCH`` (12:00 EDT) is
deliberately outside any quiet window so non-quiet tests fire cleanly.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.core import classify
from chief.core.scheduler import Scheduler
from chief.persistence.schedules import (
    ACTION_BASH,
    ACTION_MESSAGE,
    ACTION_WAKEUP,
    KIND_MONITOR,
    KIND_ONCE,
    KIND_RECURRING,
    PREDICATE_AGENT,
    create_schedule,
    get_schedule,
)
from chief.tools.browser import mcp as browser_mcp
from chief.tools.shell import ShellService
from chief.tools.web import WebService

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
    google_services: Any = (),
    monitor_model: str = "auto",
    web_service: WebService | None = None,
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
        google_services=google_services,
        monitor_model=monitor_model,
        web_service=web_service,
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

    async def fake_rc(session_id: str, command: str) -> dict[str, Any]:
        calls.append((session_id, command))
        return {"stdout": "disk ok\n", "exit_code": 0}

    shell = ShellService(
        workspace_dir="data/workspace", timeout_seconds=120.0, output_limit=64_000
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
    session_id, command = calls[0]
    assert command == "df -h"
    assert session_id == f"schedule:{sched.id}"
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

    shell = ShellService(
        workspace_dir="data/workspace", timeout_seconds=1.0, output_limit=10
    )
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

    shell = ShellService(
        workspace_dir="data/workspace", timeout_seconds=1.0, output_limit=10
    )
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


# ---- predicate evaluator: browser read-tool surface -------------------------


async def test_predicate_evaluator_includes_browser_read_tools_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wired surfaces join allowed_tools; the removed WebFetch/WebSearch never do.

    The predicate evaluator assembles the allowed surface from the chief_web tools
    (when a WebService is wired, #88 HIGH-2) plus each GoogleService's read_tools.
    Passing the browser service means browser READ_TOOLS must appear in allowed_tools
    and WRITE_TOOLS must be absent (the predicate surface bypasses the gate — reads
    only), and the removed claude built-ins WebFetch/WebSearch must appear nowhere.
    """
    captured: dict[str, Any] = {}

    async def fake_ask_condition(
        question: str,
        *,
        model: str,
        allowed_tools: list[str],
        mcp_servers: dict[str, Any] | None = None,
    ) -> bool:
        captured["model"] = model
        captured["allowed_tools"] = list(allowed_tools)
        captured["mcp_servers"] = dict(mcp_servers or {})
        return True

    monkeypatch.setattr(classify, "ask_condition", fake_ask_condition)

    browser_service = browser_mcp.service("http://mcp-playwright:3000/mcp")
    web_service = WebService()
    async with session_factory() as s:
        await create_schedule(
            s,
            kind=KIND_MONITOR,
            spec="* * * * *",
            action="notify",
            action_type=ACTION_MESSAGE,
            next_run=EPOCH,
            predicate="is the dashboard green?",
            predicate_type=PREDICATE_AGENT,
        )
    await _make(
        session_factory,
        google_services=(browser_service,),
        monitor_model="copilot/x",
        web_service=web_service,
    ).tick()

    assert "allowed_tools" in captured, "ask_condition was never called"
    # #88 HIGH-1: the monitor evaluates on the injected Copilot monitor_model, never the
    # OpenRouter classifier setting.
    assert captured["model"] == "copilot/x"
    allowed = set(captured["allowed_tools"])

    # #88 HIGH-2: with a WebService wired the monitor gets BOTH chief_web tools and the
    # chief_web server, alongside the Google reads.
    assert web_service.search_tool_name in allowed
    assert web_service.fetch_tool_name in allowed
    assert web_service.server_name in captured["mcp_servers"]

    # The removed claude built-ins must never appear.
    assert "WebFetch" not in allowed
    assert "WebSearch" not in allowed

    # All browser READ_TOOLS must be present.
    for tool in browser_mcp.READ_TOOLS:
        assert tool in allowed, (
            f"browser read tool {tool!r} missing from predicate allowed_tools"
        )

    # No browser WRITE_TOOLS may appear (predicate surface bypasses the gate).
    for tool in browser_mcp.WRITE_TOOLS:
        assert tool not in allowed, (
            f"browser write tool {tool!r} must NOT be in predicate allowed_tools"
        )

    # The playwright MCP server must be registered.
    assert "playwright" in captured["mcp_servers"]


async def test_predicate_evaluator_excludes_browser_tools_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing is wired, allowed_tools is empty (no browser, no chief_web)."""
    captured: dict[str, Any] = {}

    async def fake_ask_condition(
        question: str,
        *,
        model: str,
        allowed_tools: list[str],
        mcp_servers: dict[str, Any] | None = None,
    ) -> bool:
        captured["allowed_tools"] = list(allowed_tools)
        captured["mcp_servers"] = dict(mcp_servers or {})
        return False

    monkeypatch.setattr(classify, "ask_condition", fake_ask_condition)

    # No google_services and no web_service → allowed_tools is exactly the Google reads
    # (here, none): no browser tools, no chief_web tools, no removed built-ins.
    async with session_factory() as s:
        await create_schedule(
            s,
            kind=KIND_MONITOR,
            spec="* * * * *",
            action="notify",
            action_type=ACTION_MESSAGE,
            next_run=EPOCH,
            predicate="is the dashboard green?",
            predicate_type=PREDICATE_AGENT,
        )
    await _make(session_factory, google_services=(), web_service=None).tick()

    assert "allowed_tools" in captured, "ask_condition was never called"
    allowed = set(captured["allowed_tools"])

    assert allowed == set(), "no surface wired → allowed_tools must be empty"

    all_browser_tools = set(browser_mcp.READ_TOOLS) | set(browser_mcp.WRITE_TOOLS)
    for tool in all_browser_tools:
        assert tool not in allowed, (
            f"browser tool {tool!r} leaked into predicate surface when disabled"
        )

    # No chief_web surface and no removed built-ins when nothing is wired.
    assert "mcp__chief_web__fetch" not in allowed
    assert "mcp__chief_web__search" not in allowed
    assert "WebFetch" not in allowed
    assert "WebSearch" not in allowed

    # No playwright / chief_web MCP servers when nothing is wired.
    assert captured.get("mcp_servers", {}) == {}
