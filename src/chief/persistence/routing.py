"""Route repository — one row per job category (issue #79, part of #72).

Each :class:`~chief.persistence.models.Route` maps a job category to a
``{target_class, model}`` target. The config boot-seed and the self-config edits write
here; :class:`~chief.core.routing.RoutingStore` reads these to resolve a task's target
at spawn. ``add_route`` is idempotent on ``category`` so re-seeding never duplicates a
row (mirrors :func:`chief.persistence.policy.add_entry`).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Route


async def list_routes(session: AsyncSession) -> list[Route]:
    """Return every routing row (one per category), oldest first."""
    stmt = select(Route).order_by(Route.id)
    return list((await session.execute(stmt)).scalars())


async def get_route(session: AsyncSession, *, category: str) -> Route | None:
    """Return the route for ``category`` or ``None``."""
    stmt = select(Route).where(Route.category == category)
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_route(
    session: AsyncSession, *, category: str, target_class: str, model: str
) -> Route | None:
    """Insert the category's route unless it already exists; return it (or ``None``)."""
    if await get_route(session, category=category) is not None:
        return None
    route = Route(category=category, target_class=target_class, model=model)
    session.add(route)
    await session.commit()
    await session.refresh(route)
    return route
