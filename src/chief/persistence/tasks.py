"""Task repository — one row per conversation, keyed by ``(platform, thread_key)``.

The task engine (M2) reads/writes lifecycle ``status`` and the resumable
``sdk_session_id`` here so a restart can recover in-flight work. Statuses are plain
strings (the model stores ``status`` as text); the constants below are the vocabulary.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Task

OPEN = "open"  # session alive, idle, awaiting the next message
RUNNING = "running"  # actively generating a turn
WAITING = "waiting"  # blocked on input/approval (M3)
DONE = "done"  # finished or archived-on-idle
FAILED = "failed"
CANCELLED = "cancelled"

#: Statuses a task can no longer leave on its own — excluded from the active list.
TERMINAL = frozenset({DONE, FAILED, CANCELLED})


async def get_task(
    session: AsyncSession, *, platform: str, thread_key: str
) -> Task | None:
    """Return the task for ``(platform, thread_key)`` or ``None``."""
    stmt = select(Task).where(
        Task.platform == platform, Task.thread_key == thread_key
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_task(
    session: AsyncSession,
    *,
    platform: str,
    thread_key: str,
    tier: str,
    title: str | None = None,
    model: str | None = None,
    surface: str | None = None,
) -> Task:
    """Return the task for ``(platform, thread_key)`` or create it (status ``OPEN``).

    ``surface`` is only stored on creation (#135) — an existing row keeps whatever
    surface it was first created with, since a given ``(platform, thread_key)`` never
    changes surface once opened.
    """
    existing = await get_task(session, platform=platform, thread_key=thread_key)
    if existing is not None:
        return existing

    task = Task(
        platform=platform,
        thread_key=thread_key,
        tier=tier,
        status=OPEN,
        title=title,
        model=model,
        surface=surface,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def set_status(session: AsyncSession, task: Task, status: str) -> None:
    """Persist a new lifecycle ``status`` for ``task``."""
    task.status = status
    await session.commit()


async def set_session_id(
    session: AsyncSession, task: Task, sdk_session_id: str
) -> None:
    """Persist the resumable SDK ``session_id`` for ``task``."""
    task.sdk_session_id = sdk_session_id
    await session.commit()


async def set_task_model(
    session: AsyncSession, task: Task, model: str | None
) -> None:
    """Persist (or clear) the per-task model override (owner Opus escalation, M11)."""
    task.model = model
    await session.commit()


async def set_route_category(
    session: AsyncSession, task: Task, category: str | None
) -> None:
    """Persist (or clear) the per-task ``/route`` category override (#79)."""
    task.route_category = category
    await session.commit()


async def get_active_account(session: AsyncSession, task: Task) -> str | None:
    """Return the active Google account label for ``task``, or ``None`` if unset."""
    return task.active_account


async def set_active_account(
    session: AsyncSession, task: Task, label: str | None
) -> None:
    """Persist (or clear) the per-thread active Google account label (issue #45)."""
    task.active_account = label
    await session.commit()


async def list_active(
    session: AsyncSession, *, platform: str | None = None
) -> list[Task]:
    """Return non-terminal tasks (open/running/waiting), oldest first."""
    stmt = select(Task).where(Task.status.not_in(TERMINAL)).order_by(Task.id)
    if platform is not None:
        stmt = stmt.where(Task.platform == platform)
    return list((await session.execute(stmt)).scalars())
