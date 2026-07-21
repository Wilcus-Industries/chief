"""Cron service: persists schedules and wakes the agent when they fire."""
# styleguide: file-length — one cohesive schedule service; the persistence CRUD
# and the fire loop share _factory/_wake/_quiet/_anchor state, so splitting them
# scatters the lifecycle across files for no real gain.

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from chief.adapters.base import Message
from chief.cron.timing import QuietHours, next_fire, utcnow, validate_spec
from chief.monitors.service import WakeAgent
from chief.persistence.db import SessionFactory
from chief.persistence.models import ScheduleRow
from chief.persistence.store import MessageStore

logger = logging.getLogger(__name__)

# How often the loop re-reads schedules while idle, so new ones are noticed.
DEFAULT_POLL_SECONDS = 30.0

#: Runs one command on a named shell thread, returning the shell result dict
#: (``ShellService.run``). A command schedule cannot fire without one.
RunCommand = Callable[[str, str], Awaitable[dict[str, Any]]]


class CronService:
    """Runs the schedule loop; schedules live in the database."""

    def __init__(
        self,
        factory: SessionFactory,
        wake: WakeAgent,
        quiet: QuietHours | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        run_command: RunCommand | None = None,
    ) -> None:
        self._factory = factory
        self.store = MessageStore(factory)  # the tool resolves wake targets here
        self._wake = wake
        self._quiet = quiet
        self._poll = poll_seconds
        self._run_command = run_command
        self._task: asyncio.Task[None] | None = None
        # Per-schedule anchor: last fire (or first sighting); next-fire is
        # computed from here so restarts don't replay missed fires.
        self._anchor: dict[int, datetime] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def create(
        self,
        *,
        description: str,
        spec: str,
        wake_channel: str,
        wake_thread: str,
        prompt: str = "",
        command: str | None = None,
    ) -> int:
        validate_spec(spec)
        async with self._factory() as db:
            row = ScheduleRow(
                description=description,
                spec=spec,
                wake_channel=wake_channel,
                wake_thread=wake_thread,
                prompt=prompt,
                command=command,
            )
            db.add(row)
            await db.commit()
            return row.id

    async def retarget(
        self, schedule_id: int, target: str | None,
        default_channel: str, default_thread: str,
    ) -> tuple[str, str]:
        """Re-point a schedule's wake. Returns (status, wake_thread), status
        ``ok`` | ``missing`` | ``command`` | ``unknown-target``. Resolve runs
        only after the checks, so a rejected retarget writes nothing."""
        async with self._factory() as db:
            row = await db.get(ScheduleRow, schedule_id)
            if row is None:
                return "missing", ""
            if row.command:  # command rows wake no session
                return "command", ""
            resolved = await self.store.resolve_wake_target(
                target, default_channel, default_thread
            )
            if resolved is None:
                return "unknown-target", ""
            row.wake_channel, row.wake_thread = resolved
            await db.commit()
            return "ok", resolved[1]

    async def list_enabled(self) -> list[ScheduleRow]:
        async with self._factory() as db:
            rows = await db.scalars(
                select(ScheduleRow).where(ScheduleRow.enabled).order_by(ScheduleRow.id)
            )
            return list(rows)

    async def delete(self, schedule_id: int) -> bool:
        async with self._factory() as db:
            row = await db.get(ScheduleRow, schedule_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    async def _loop(self) -> None:
        while True:
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick — a schema skew after an upgrade, a transient DB
                # error — must not silently kill the loop and stop every
                # schedule until the daemon restarts. Log it and retry.
                logger.exception("schedule tick failed; retrying")
                delay = self._poll
            await asyncio.sleep(min(delay, self._poll))

    async def _tick(self) -> float:
        """Fire everything due now; return seconds until the next fire."""
        now = utcnow()
        soonest = self._poll
        for schedule in await self.list_enabled():
            try:
                fire = await self._advance(schedule, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A row whose spec predates validation (or was written by hand)
                # raises every tick. Skipping it keeps one poison row from
                # starving every schedule after it in the list.
                logger.exception(
                    "schedule %s skipped: %s", schedule.id, schedule.spec
                )
                continue
            soonest = min(soonest, (fire - now).total_seconds())
        return max(soonest, 0.01)

    async def _advance(self, schedule: ScheduleRow, now: datetime) -> datetime:
        """Fire ``schedule`` if it is due; return when it fires next."""
        anchor = self._anchor.setdefault(schedule.id, now)
        # Quiet hours protect the owner's attention; a silent command has none
        # to protect, so only prompt rows defer.
        quiet = None if schedule.command else self._quiet
        fire = next_fire(schedule.spec, anchor, quiet)
        if fire <= now:
            await self._fire(schedule)
            self._anchor[schedule.id] = now
            fire = next_fire(schedule.spec, now, quiet)
        return fire

    async def _fire(self, schedule: ScheduleRow) -> None:
        logger.info("schedule %s fired: %s", schedule.id, schedule.description)
        if schedule.command:
            await self._run_scheduled_command(schedule.id, schedule.command)
            return
        text = f"[schedule #{schedule.id}: {schedule.description}]\n{schedule.prompt}"
        await self._wake(
            Message(
                channel=schedule.wake_channel,
                sender="system",
                thread_key=schedule.wake_thread,
                text=text,
            )
        )

    async def _run_scheduled_command(self, schedule_id: int, command: str) -> None:
        """Run a command row on its own shell thread, ``cron:<id>``.

        The dedicated thread keeps scheduled work from mutating a
        conversation's persistent shell (cwd, environment).
        """
        if self._run_command is None:
            logger.error(
                "schedule %s has a command but no shell runner is wired", schedule_id
            )
            return
        try:
            result = await self._run_command(f"cron:{schedule_id}", command)
        except Exception:
            # Nobody is present to see this fail; one bad command must not
            # take the whole schedule loop down with it.
            logger.exception("schedule %s command failed: %s", schedule_id, command)
            return
        logger.info(
            "schedule %s command exit=%s", schedule_id, result.get("exit_code")
        )
