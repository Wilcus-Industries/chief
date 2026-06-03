"""Approval repository — one row per permission-gate request (owned by M3).

The gate parks an effectful tool call on an :class:`~chief.persistence.models.Approval`
row while it waits for the owner's button tap. States move
``requested → notified → approved | denied | timed_out | cancelled``; the terminal set
is closed (a decided approval never reopens). ``list_pending`` feeds boot-time re-arm so
a tap that lands after a restart still records a decision.
"""

from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Approval, _utcnow

REQUESTED = "requested"  # row created, card not yet posted
NOTIFIED = "notified"  # card posted, awaiting a decision
APPROVED = "approved"
DENIED = "denied"
TIMED_OUT = "timed_out"  # no decision within the window → treated as deny
CANCELLED = "cancelled"

#: Still awaiting a decision — re-armed on boot.
PENDING = frozenset({REQUESTED, NOTIFIED})
#: Decided — never reopens.
TERMINAL = frozenset({APPROVED, DENIED, TIMED_OUT, CANCELLED})


async def create_approval(
    session: AsyncSession,
    *,
    task_id: int | None,
    kind: str,
    payload_preview: str | None = None,
) -> Approval:
    """Insert a fresh ``requested`` approval and return it (with its id)."""
    approval = Approval(task_id=task_id, kind=kind, payload_preview=payload_preview)
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return approval


async def get(session: AsyncSession, approval_id: int) -> Approval | None:
    """Return the approval by id, or ``None``."""
    return await session.get(Approval, approval_id)


async def set_state(
    session: AsyncSession,
    approval: Approval,
    state: str,
    *,
    decided_by: str | None = None,
) -> None:
    """Persist ``state``; stamp ``decided_*`` when the state is terminal."""
    approval.state = state
    if state in TERMINAL:
        approval.decided_by = decided_by
        approval.decided_at = _utcnow()
    await session.commit()


async def list_pending(session: AsyncSession) -> list[Approval]:
    """Return undecided approvals (``requested``/``notified``), oldest first."""
    stmt = select(Approval).where(Approval.state.in_(PENDING)).order_by(Approval.id)
    return list((await session.execute(stmt)).scalars())


async def try_decide(
    session: AsyncSession,
    approval_id: int,
    state: str,
    *,
    decided_by: str | None,
) -> bool:
    """Atomically move a *pending* approval to terminal ``state``; stamp the decider.

    Returns ``True`` only for the writer that flipped it — a single ``UPDATE … WHERE
    state IN (pending)`` so two racing taps (or a tap racing the timeout) cannot both
    decide the same row. ``False`` if it was already terminal or unknown.
    """
    stmt = (
        update(Approval)
        .where(Approval.id == approval_id, Approval.state.in_(PENDING))
        .values(state=state, decided_by=decided_by, decided_at=_utcnow())
    )
    result = cast(CursorResult[Any], await session.execute(stmt))
    await session.commit()
    return bool(result.rowcount)
