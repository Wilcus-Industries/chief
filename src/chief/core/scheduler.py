"""Scheduler engine (M9a) — fires reminders, recurring jobs, and self-cron.

chief is otherwise reactive; this is the one long-running loop that acts *unprompted*,
on items the owner set up (DESIGN §"Proactivity, scheduling & skills"). A single
coroutine owns every schedule read and write, so a tick never races itself or another —
no locking needed.

:meth:`tick` pulls every due schedule (:func:`persistence.schedules.list_due`,
``next_run <= now``) and, for each, either DEFERS or FIRES it:

- **Defer.** A non-``urgent`` fire caught inside the owner's quiet hours is pushed to
  the next ``quiet_end`` (:func:`schedule_time.defer_target`) without firing.
- **Fire, then advance.** ``recurring`` advances to the next cron occurrence *after now*
  — so a slot missed during downtime fires once on restart rather than replaying every
  miss — while ``once`` is disabled.

Delivery is **at-least-once**: a fire happens before ``next_run`` advances, so a crash
in the window between them re-fires the row on the next tick (a rare owner-visible
duplicate). The alternative — advance first — would instead drop a fire on a crash, and
a missed reminder is worse than a repeated one. A malformed/unsatisfiable cron ``spec``
(``next_fire`` → ``None``) disables the row rather than looping on the advance.

Three fire paths, by ``action_type``:

- ``message`` — :meth:`SchedulerIO.send` to the target. No model, free.
- ``wakeup``  — :meth:`Waker.wake`, which boots a full agent turn that re-passes the
  permission gate, so any effectful tool it reaches still raises an approval card.
- ``bash``    — runs the command in the M7 sandbox (no agent), delivering output the way
  ``tasks.py`` delivers a long reply (a file when big, else split messages). Creating a
  bash schedule is itself the gated act (``tools/schedule.py``), since the fire bypasses
  the per-turn gate.

A parallel heartbeat loop (:meth:`heartbeat_once`) GETs ``heartbeat_url`` every
``heartbeat_interval_seconds`` as a dead-man's-switch; ping failures are logged, never
fatal. Firing happens outside any open DB transaction, so a slow ``bash`` never holds a
sqlite connection while it runs.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.base import (
    FILE_REPLY_NOTE,
    reply_filename,
    should_send_as_file,
)
from ..persistence.models import Schedule
from ..persistence.schedules import (
    ACTION_BASH,
    ACTION_MESSAGE,
    ACTION_WAKEUP,
    KIND_RECURRING,
    disable_schedule,
    get_schedule,
    list_due,
    set_last_run,
    set_next_run,
)
from ..tools.shell import ShellService, format_result
from ..tools.shell import run_command as _run_command
from .schedule_time import defer_target, in_quiet_hours, next_fire

logger = logging.getLogger("chief.core.scheduler")

#: Per-ping request budget for the dead-man's-switch GET (short; a slow ping is a fail).
_HEARTBEAT_TIMEOUT = 10.0

RunCommand = Callable[..., Awaitable[dict[str, Any]]]


class SchedulerIO(Protocol):
    """The slice of the platform IO the scheduler delivers through."""

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None: ...


class Waker(Protocol):
    """The wakeup entry (implemented by :class:`~chief.core.tasks.TaskManager`)."""

    async def wake(self, *, thread_key: str, text: str) -> None: ...


class HttpClient(Protocol):
    """The slice of ``httpx.AsyncClient`` the heartbeat uses."""

    async def get(self, url: str, *, timeout: float) -> object: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Scheduler:
    """The long-running tick + heartbeat loops that drive scheduled work."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        io: SchedulerIO,
        waker: Waker,
        primary_thread_key: str,
        shell_service: ShellService | None = None,
        run_command: RunCommand = _run_command,
        owner_tz: str = "UTC",
        quiet_hours_start: str | None = None,
        quiet_hours_end: str = "07:00",
        tick_seconds: float = 30.0,
        message_limit: int = 4096,
        heartbeat_url: str | None = None,
        heartbeat_interval_seconds: int = 300,
        http: HttpClient | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._io = io
        self._waker = waker
        self._primary_thread_key = primary_thread_key
        self._shell_service = shell_service
        self._run_command = run_command
        self._tz = ZoneInfo(owner_tz)
        self._quiet_start = quiet_hours_start
        self._quiet_end = quiet_hours_end
        self._tick_seconds = tick_seconds
        self._message_limit = message_limit
        self._heartbeat_url = heartbeat_url
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._http = http
        self._now = now

    # ---- loops -----------------------------------------------------------

    async def run(self) -> None:
        """Run the tick and heartbeat loops until cancelled (added to app's gather)."""
        await asyncio.gather(self._tick_loop(), self._heartbeat_loop())

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")

    async def _heartbeat_loop(self) -> None:
        if self._heartbeat_url is None:
            return
        while True:
            await self.heartbeat_once()
            await asyncio.sleep(self._heartbeat_interval_seconds)

    # ---- one pass (public for tests + a future manual trigger) -----------

    async def tick(self) -> None:
        """Process every due schedule once: defer, or fire then advance."""
        now = self._now()
        async with self._session_factory() as session:
            due = await list_due(session, now=now)
        # The session is closed before firing — a slow bash must not hold a connection.
        # The rows stay readable (loaded, no commit), and writes re-fetch by id below.
        for schedule in due:
            await self._process(schedule, now)

    async def heartbeat_once(self) -> None:
        """GET the dead-man's-switch url once; a failure is logged, never raised."""
        if self._heartbeat_url is None or self._http is None:
            return
        try:
            await self._http.get(self._heartbeat_url, timeout=_HEARTBEAT_TIMEOUT)
        except Exception:
            logger.warning("heartbeat ping failed", exc_info=True)

    # ---- per-schedule ----------------------------------------------------

    async def _process(self, schedule: Schedule, now: datetime) -> None:
        if not schedule.urgent and in_quiet_hours(
            now, self._quiet_start, self._quiet_end, self._tz
        ):
            # Defer without firing — released at the next quiet_end.
            await self._update_next_run(
                schedule.id, defer_target(now, self._quiet_end, self._tz)
            )
            return
        await self._fire(schedule)
        await self._record_run(schedule.id, schedule.kind, schedule.spec, now)

    async def _fire(self, schedule: Schedule) -> None:
        target = schedule.thread_key or self._primary_thread_key
        action = schedule.action or ""
        if schedule.action_type == ACTION_MESSAGE:
            await self._io.send(target, action)
        elif schedule.action_type == ACTION_WAKEUP:
            await self._waker.wake(thread_key=target, text=action)
        elif schedule.action_type == ACTION_BASH:
            await self._fire_bash(schedule.id, target, action)
        else:
            logger.warning(
                "schedule %s has unknown action_type %r",
                schedule.id,
                schedule.action_type,
            )

    async def _fire_bash(self, schedule_id: int, target: str, command: str) -> None:
        shell = self._shell_service
        if shell is None:
            # Shell disabled but a bash schedule survived — skip the run (still advances
            # in _record_run, so it can't hot-loop) and flag the misconfiguration.
            logger.warning(
                "schedule %s is bash but the shell is disabled; skipping run",
                schedule_id,
            )
            return
        try:
            result = await self._run_command(
                shell.host,
                shell.port,
                f"schedule:{schedule_id}",
                command,
                read_timeout=shell.read_timeout,
            )
        except Exception as exc:  # sandbox down/unreachable — surface, don't crash
            logger.warning("scheduled bash failed (schedule %s): %s", schedule_id, exc)
            await self._io.send(target, f"⚠️ scheduled command failed: {exc}")
            return
        text = str(format_result(result)["content"][0]["text"])
        body = f"⏰ `{command}`\n{text}"
        if should_send_as_file(body, self._message_limit):
            await self._io.send_file(
                target, reply_filename(), body.encode("utf-8"), caption=FILE_REPLY_NOTE
            )
        else:
            await self._io.send(target, body)

    async def _record_run(
        self, schedule_id: int, kind: str, spec: str, now: datetime
    ) -> None:
        """Stamp last_run and advance: recurring → next cron, once → disabled."""
        async with self._session_factory() as session:
            schedule = await get_schedule(session, schedule_id)
            if schedule is None:
                return
            await set_last_run(session, schedule, now)
            if kind == KIND_RECURRING:
                nxt = next_fire(KIND_RECURRING, spec, after=now, tz=self._tz)
                if nxt is None:  # unparseable cron — disable rather than loop
                    await disable_schedule(session, schedule)
                else:
                    await set_next_run(session, schedule, nxt)
            else:
                await disable_schedule(session, schedule)

    async def _update_next_run(self, schedule_id: int, next_run: datetime) -> None:
        async with self._session_factory() as session:
            schedule = await get_schedule(session, schedule_id)
            if schedule is not None:
                await set_next_run(session, schedule, next_run)
