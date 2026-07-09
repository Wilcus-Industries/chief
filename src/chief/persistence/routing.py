"""Route repository — one row per job category (issue #79, part of #72).

Each :class:`~chief.persistence.models.Route` maps a job category to a
``{target_class, model}`` target plus an optional description. The config boot-seed and
the self-config edits (#83) write here; :class:`~chief.core.routing.RoutingStore` reads
these to resolve a task's target at spawn. ``add_route`` is idempotent on ``category``
so re-seeding never duplicates a row (mirrors :func:`chief.persistence.policy`).
``set_route_target`` / ``set_route_description`` / ``rename_route`` / ``remove_route``
are the runtime edits the self-config tool drives.
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
    session: AsyncSession,
    *,
    category: str,
    target_class: str,
    model: str,
    description: str | None = None,
) -> Route | None:
    """Insert the category's route unless it already exists; return it (or ``None``)."""
    if await get_route(session, category=category) is not None:
        return None
    route = Route(
        category=category,
        target_class=target_class,
        model=model,
        description=description,
    )
    session.add(route)
    await session.commit()
    await session.refresh(route)
    return route


async def set_route_target(
    session: AsyncSession, *, category: str, target_class: str, model: str
) -> Route | None:
    """Repoint ``category`` at ``{target_class, model}``; ``None`` if it has no row."""
    route = await get_route(session, category=category)
    if route is None:
        return None
    route.target_class = target_class
    route.model = model
    await session.commit()
    await session.refresh(route)
    return route


async def set_route_description(
    session: AsyncSession, *, category: str, description: str | None
) -> Route | None:
    """Set (or clear) ``category``'s description; ``None`` if it has no row."""
    route = await get_route(session, category=category)
    if route is None:
        return None
    route.description = description
    await session.commit()
    await session.refresh(route)
    return route


async def rename_route(
    session: AsyncSession, *, old: str, new: str
) -> Route | None:
    """Rename category ``old`` to ``new`` in place; ``None`` if ``old`` has no row.

    The caller must have already ensured ``new`` is free (the unique constraint would
    otherwise raise on commit); :class:`~chief.core.routing.RoutingStore` checks first.
    """
    route = await get_route(session, category=old)
    if route is None:
        return None
    route.category = new
    await session.commit()
    await session.refresh(route)
    return route


async def remove_route(session: AsyncSession, *, category: str) -> bool:
    """Delete ``category``'s row; return whether a row was actually removed."""
    route = await get_route(session, category=category)
    if route is None:
        return False
    await session.delete(route)
    await session.commit()
    return True
