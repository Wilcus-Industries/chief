"""Budget choice-card wire format: payload, parse_budget, apply_budget_decision."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import (
    BudgetAction,
    apply_budget_decision,
    budget_payload,
    parse_budget,
)
from chief.persistence import usage


def test_budget_payload_round_trips() -> None:
    payload = budget_payload("2026-06", BudgetAction.DOWNGRADE)
    assert payload == "bud:2026-06:downgrade"
    assert parse_budget(payload) == ("2026-06", BudgetAction.DOWNGRADE)


def test_parse_budget_round_trips_every_action() -> None:
    for action in BudgetAction:
        assert parse_budget(budget_payload("2026-06", action)) == ("2026-06", action)


def test_parse_budget_rejects_foreign_or_malformed() -> None:
    assert parse_budget("adm:1:admit") is None
    assert parse_budget("bud:2026-06") is None
    assert parse_budget("bud:2026-06:bogus") is None
    assert parse_budget("bud:2026-06:downgrade:extra") is None


async def test_apply_budget_decision_flips_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = {
        BudgetAction.DOWNGRADE: usage.MODE_DOWNGRADED,
        BudgetAction.CONTINUE: usage.MODE_CONTINUE,
        BudgetAction.OVERFLOW: usage.MODE_OVERFLOW,
    }
    for action, expected in cases.items():
        mode = await apply_budget_decision(
            session_factory, cycle="2026-06", action=action
        )
        assert mode == expected
        async with session_factory() as session:
            row = await usage.get_row(session, "2026-06")
        assert row is not None and row.mode == expected
