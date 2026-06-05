"""Schedule repository — reminders, recurring jobs, and self-cron (M9a).

One row per scheduled item, fired by the long-running scheduler tick (a follow-up
milestone). Mirrors :mod:`chief.persistence.tasks`: module-level async fns over an
``AsyncSession``, with the schedule vocabulary (``kind`` / ``action_type``) as constants
here so the engine and the owner's schedule tools share one source of truth.

``kind`` picks the next-run advance logic; ``action_type`` picks the fire path. The two
are orthogonal — any trigger kind can drive any action type. Timestamps (``next_run`` /
``last_run``) are stored in UTC; sqlite round-trips them naive, so a reader needing an
aware value re-stamps UTC (see :mod:`chief.persistence.rate_limits`).
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Schedule

#: Trigger kinds — how :func:`next_run` advances after a fire.
KIND_ONCE = "once"  # fires once at an ISO timestamp, then disables
KIND_RECURRING = "recurring"  # fires on a cron expression, re-advancing each time
KIND_MONITOR = "monitor"  # checks a predicate on a cron cadence, fires on a flip

#: Action types — what a fire does (the dispatch path in the scheduler).
ACTION_MESSAGE = "message"  # direct text send, no model, free
ACTION_WAKEUP = "wakeup"  # inject a full agent turn (gate + tools apply)
ACTION_BASH = "bash"  # run a command in the sandbox, no agent

#: Predicate types — how a monitor evaluates its watched condition each cadence tick.
PREDICATE_BASH = "bash"  # a sandbox command; exit 0 ⇒ true
PREDICATE_AGENT = "agent"  # a read-only Haiku yes/no judgment


async def create_schedule(
    session: AsyncSession,
    *,
    kind: str,
    spec: str,
    action: str,
    action_type: str,
    next_run: datetime,
    thread_key: str | None = None,
    urgent: bool = False,
    predicate: str | None = None,
    predicate_type: str | None = None,
) -> Schedule:
    """Insert a schedule and return it (enabled, ``next_run`` set).

    ``predicate`` / ``predicate_type`` are set only for ``monitor`` rows (the watched
    condition); they stay ``None`` for ``once`` / ``recurring``.
    """
    schedule = Schedule(
        kind=kind,
        spec=spec,
        action=action,
        action_type=action_type,
        next_run=next_run,
        thread_key=thread_key,
        urgent=urgent,
        predicate=predicate,
        predicate_type=predicate_type,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def get_schedule(session: AsyncSession, schedule_id: int) -> Schedule | None:
    """Return the schedule with ``schedule_id`` or ``None``."""
    return await session.get(Schedule, schedule_id)


async def list_due(session: AsyncSession, *, now: datetime) -> list[Schedule]:
    """Return enabled schedules whose ``next_run`` is at or before ``now`` (``<=``).

    The boundary is inclusive so a schedule landing exactly on a tick fires that tick.
    Ordered by ``next_run`` (oldest first) so a restart catch-up fires in time order.
    """
    stmt = (
        select(Schedule)
        .where(
            Schedule.enabled,
            Schedule.next_run.is_not(None),
            Schedule.next_run <= now,
        )
        .order_by(Schedule.next_run, Schedule.id)
    )
    return list((await session.execute(stmt)).scalars())


async def list_enabled(session: AsyncSession) -> list[Schedule]:
    """Return every enabled schedule (next fire first) — for the ``/schedules`` view.

    Every created schedule has ``next_run`` set, so the sort never meets a NULL here
    (which sqlite would sort first); ``create_schedule`` upholds that invariant.
    """
    stmt = (
        select(Schedule)
        .where(Schedule.enabled)
        .order_by(Schedule.next_run, Schedule.id)
    )
    return list((await session.execute(stmt)).scalars())


async def set_next_run(
    session: AsyncSession, schedule: Schedule, next_run: datetime
) -> None:
    """Persist the next fire time for ``schedule`` (recurring advance / quiet defer)."""
    schedule.next_run = next_run
    await session.commit()


async def set_last_run(
    session: AsyncSession, schedule: Schedule, last_run: datetime
) -> None:
    """Record the most recent fire time for ``schedule``."""
    schedule.last_run = last_run
    await session.commit()


async def set_last_result(
    session: AsyncSession, schedule: Schedule, result: bool
) -> None:
    """Persist a monitor's latest predicate truth (drives false→true flip detection)."""
    schedule.last_result = result
    await session.commit()


async def disable_schedule(session: AsyncSession, schedule: Schedule) -> None:
    """Disable ``schedule`` — a fired one-off, or an owner cancel."""
    schedule.enabled = False
    await session.commit()
