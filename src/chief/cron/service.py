"""Cron service: persists schedules and wakes the agent when they fire."""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from chief.adapters.base import Message
from chief.cron.timing import QuietHours, next_fire, utcnow
from chief.monitors.service import WakeAgent
from chief.persistence.db import SessionFactory
from chief.persistence.models import ScheduleRow

logger = logging.getLogger(__name__)

# How often the loop re-reads schedules while idle, so new ones are noticed.
DEFAULT_POLL_SECONDS = 30.0


class CronService:
    """Runs the schedule loop; schedules live in the database."""

    def __init__(
        self,
        factory: SessionFactory,
        wake: WakeAgent,
        quiet: QuietHours | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._factory = factory
        self._wake = wake
        self._quiet = quiet
        self._poll = poll_seconds
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
        prompt: str,
    ) -> int:
        async with self._factory() as db:
            row = ScheduleRow(
                description=description,
                spec=spec,
                wake_channel=wake_channel,
                wake_thread=wake_thread,
                prompt=prompt,
            )
            db.add(row)
            await db.commit()
            return row.id

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
            delay = await self._tick()
            await asyncio.sleep(min(delay, self._poll))

    async def _tick(self) -> float:
        """Fire everything due now; return seconds until the next fire."""
        now = utcnow()
        soonest = self._poll
        for schedule in await self.list_enabled():
            anchor = self._anchor.setdefault(schedule.id, now)
            fire = next_fire(schedule.spec, anchor, self._quiet)
            if fire <= now:
                await self._fire(schedule)
                self._anchor[schedule.id] = now
                fire = next_fire(schedule.spec, now, self._quiet)
            soonest = min(soonest, (fire - now).total_seconds())
        return max(soonest, 0.01)

    async def _fire(self, schedule: ScheduleRow) -> None:
        logger.info("schedule %s fired: %s", schedule.id, schedule.description)
        text = f"[schedule #{schedule.id}: {schedule.description}]\n{schedule.prompt}"
        await self._wake(
            Message(
                channel=schedule.wake_channel,
                sender="system",
                thread_key=schedule.wake_thread,
                text=text,
            )
        )
