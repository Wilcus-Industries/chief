"""Budget: OpenRouter dollars per calendar-month cycle, persisted spend.

Exhaustion downgrades the model when a ``downgrade`` role is configured,
otherwise turns are refused until the next cycle.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import func, select

from chief.persistence.db import SessionFactory
from chief.persistence.models import SpendRow


class BudgetState(Enum):
    OK = "ok"
    WARN = "warn"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class BudgetStatus:
    state: BudgetState
    spent: float
    cap: float


def _cycle_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Budget:
    """Reads and records spend; a cap of 0 means unlimited."""

    def __init__(
        self, factory: SessionFactory, cap_usd: float, warn_ratio: float = 0.8
    ) -> None:
        self._factory = factory
        self._cap = cap_usd
        self._warn_ratio = warn_ratio

    async def status(self) -> BudgetStatus:
        """Where this cycle's spend stands against the cap."""
        spent = await self._spent_since(_cycle_start())
        if self._cap <= 0:
            return BudgetStatus(BudgetState.OK, spent, self._cap)
        if spent >= self._cap:
            return BudgetStatus(BudgetState.EXHAUSTED, spent, self._cap)
        if spent >= self._cap * self._warn_ratio:
            return BudgetStatus(BudgetState.WARN, spent, self._cap)
        return BudgetStatus(BudgetState.OK, spent, self._cap)

    async def record(self, thread_key: str, cost: float) -> None:
        """Persist one turn's dollar cost."""
        if cost <= 0:
            return
        async with self._factory() as db:
            db.add(SpendRow(thread_key=thread_key, cost=cost))
            await db.commit()

    async def _spent_since(self, start: datetime) -> float:
        async with self._factory() as db:
            total = await db.scalar(
                select(func.sum(SpendRow.cost)).where(SpendRow.created_at >= start)
            )
            return float(total or 0.0)
