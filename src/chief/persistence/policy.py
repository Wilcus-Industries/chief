"""Policy repository — the NEVER/APPROVED allowlist rows (owned by M3).

Each :class:`~chief.persistence.models.PolicyEntry` is a safe-matched ``(tool,
arg_pattern)`` on one of the two lists. The gate reads these to classify a tool call;
the self-curating "always allow/deny" buttons add to them. ``entry_exists`` keeps both
the config seed and repeated taps idempotent (no duplicate rules).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PolicyEntry

NEVER = "NEVER"
APPROVED = "APPROVED"


async def list_entries(session: AsyncSession, list_name: str) -> list[PolicyEntry]:
    """Return every entry on ``list_name`` (``NEVER`` or ``APPROVED``), oldest first."""
    stmt = (
        select(PolicyEntry)
        .where(PolicyEntry.list_name == list_name)
        .order_by(PolicyEntry.id)
    )
    return list((await session.execute(stmt)).scalars())


async def entry_exists(
    session: AsyncSession, *, list_name: str, tool: str, arg_pattern: str | None
) -> bool:
    """Return whether an identical ``(list_name, tool, arg_pattern)`` row exists."""
    stmt = select(PolicyEntry.id).where(
        PolicyEntry.list_name == list_name,
        PolicyEntry.tool == tool,
        PolicyEntry.arg_pattern.is_(arg_pattern)
        if arg_pattern is None
        else PolicyEntry.arg_pattern == arg_pattern,
    )
    return (await session.execute(stmt)).first() is not None


async def add_entry(
    session: AsyncSession, *, list_name: str, tool: str, arg_pattern: str | None
) -> PolicyEntry | None:
    """Insert the entry unless an identical one exists; return it (or ``None``)."""
    if await entry_exists(
        session, list_name=list_name, tool=tool, arg_pattern=arg_pattern
    ):
        return None
    entry = PolicyEntry(list_name=list_name, tool=tool, arg_pattern=arg_pattern)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry
