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
- **Monitor.** A ``monitor`` row treats its cron ``spec`` as a check *cadence*: each due
  tick evaluates its predicate (a sandbox command, or a read-only Haiku yes/no) and
  fires the action only on a false→true flip, then advances like ``recurring``. A
  non-urgent flip caught inside quiet hours is deferred coarsely (noticed at the next
  ``quiet_end``), so a transient quiet-window flip can be missed.

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
from collections.abc import Awaitable, Callable, Sequence
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
    KIND_MONITOR,
    KIND_RECURRING,
    PREDICATE_AGENT,
    PREDICATE_BASH,
    disable_schedule,
    get_schedule,
    list_due,
    set_last_result,
    set_last_run,
    set_next_run,
)
from ..tools.google import GoogleService
from ..tools.shell import ShellService, format_result
from ..tools.shell import run_command as _run_command
from . import classify
from .schedule_time import defer_target, in_quiet_hours, next_fire

logger = logging.getLogger("chief.core.scheduler")

#: Per-ping request budget for the dead-man's-switch GET (short; a slow ping is a fail).
_HEARTBEAT_TIMEOUT = 10.0

#: Read-only web tools an agent monitor always gets (plus the owner's Google reads).
_WEB_READ_TOOLS = ("WebFetch", "WebSearch")

RunCommand = Callable[..., Awaitable[dict[str, Any]]]
#: An agent monitor's predicate: judge whether ``question`` holds now (injectable so
#: tests skip the live model; the default builds a read-only Haiku call in __init__).
AgentPredicate = Callable[[str], Awaitable[bool]]


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
        google_services: Sequence[GoogleService] = (),
        classifier_model: str = "claude-haiku-4-5",
        agent_predicate: AgentPredicate | None = None,
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
        self._google_services = tuple(google_services)
        self._classifier_model = classifier_model
        self._agent_predicate = agent_predicate
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
            # Defer without firing — released at the next quiet_end. A monitor gets the
            # same coarse treatment: a flip inside quiet hours is noticed at quiet_end.
            await self._update_next_run(
                schedule.id, defer_target(now, self._quiet_end, self._tz)
            )
            return
        if schedule.kind == KIND_MONITOR:
            await self._evaluate_monitor(schedule)
        else:
            await self._fire(schedule)
        await self._record_run(schedule.id, schedule.kind, schedule.spec, now)

    async def _evaluate_monitor(self, schedule: Schedule) -> None:
        """Check a monitor's predicate; fire its action only on a false→true flip."""
        result = await self._evaluate_predicate(schedule)
        # Flip = the condition is true now and was not last time (None counts as "not
        # true", so the first true after creation fires). A standing true won't re-fire.
        if result and schedule.last_result is not True:
            await self._fire(schedule)
        await self._set_last_result(schedule.id, result)

    async def _evaluate_predicate(self, schedule: Schedule) -> bool:
        """Resolve a monitor's predicate to a bool (fail-safe ``False`` on error)."""
        predicate = schedule.predicate or ""
        if schedule.predicate_type == PREDICATE_BASH:
            return await self._evaluate_bash_predicate(schedule.id, predicate)
        if schedule.predicate_type == PREDICATE_AGENT:
            return await self._evaluate_agent_predicate(predicate)
        logger.warning(
            "monitor %s has unknown predicate_type %r",
            schedule.id,
            schedule.predicate_type,
        )
        return False

    async def _evaluate_bash_predicate(self, schedule_id: int, command: str) -> bool:
        """True iff the sandbox command exits 0; ``False`` if the shell is off/down."""
        shell = self._shell_service
        if shell is None:
            logger.warning(
                "monitor %s predicate is bash but the shell is disabled; skipping",
                schedule_id,
            )
            return False
        try:
            result = await self._run_command(
                shell.host,
                shell.port,
                f"monitor:{schedule_id}",
                command,
                read_timeout=shell.read_timeout,
            )
        except Exception as exc:  # sandbox down/unreachable — treat as "not true"
            logger.warning(
                "monitor %s predicate failed: %s (treating as false)", schedule_id, exc
            )
            return False
        return int(result.get("exit_code", 0)) == 0

    async def _evaluate_agent_predicate(self, question: str) -> bool:
        """A read-only Haiku yes/no over the owner's web + Google read surface."""
        if self._agent_predicate is not None:  # injected (tests) — skip the live model
            return await self._agent_predicate(question)
        allowed = list(_WEB_READ_TOOLS)
        mcp_servers: dict[str, Any] = {}
        for svc in self._google_services:
            allowed += list(svc.read_tools)  # reads only (this turn bypasses the gate)
            mcp_servers[svc.server_name] = svc.server_config()
        return await classify.ask_condition(
            question,
            model=self._classifier_model,
            allowed_tools=allowed,
            mcp_servers=mcp_servers,
        )

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
        """Stamp last_run and advance: any cron cadence → next occurrence, once → off.

        ``recurring`` and ``monitor`` share the cron-cadence advance (a monitor's
        ``now`` is its last *check*); an unparseable cadence disables, never loops.
        ``once`` always disables after its single fire.
        """
        async with self._session_factory() as session:
            schedule = await get_schedule(session, schedule_id)
            if schedule is None:
                return
            await set_last_run(session, schedule, now)
            if kind in (KIND_RECURRING, KIND_MONITOR):
                nxt = next_fire(kind, spec, after=now, tz=self._tz)
                if nxt is None:  # unparseable cron — disable rather than loop
                    await disable_schedule(session, schedule)
                else:
                    await set_next_run(session, schedule, nxt)
            else:
                await disable_schedule(session, schedule)

    async def _set_last_result(self, schedule_id: int, result: bool) -> None:
        async with self._session_factory() as session:
            schedule = await get_schedule(session, schedule_id)
            if schedule is not None:
                await set_last_result(session, schedule, result)

    async def _update_next_run(self, schedule_id: int, next_run: datetime) -> None:
        async with self._session_factory() as session:
            schedule = await get_schedule(session, schedule_id)
            if schedule is not None:
                await set_next_run(session, schedule, next_run)
