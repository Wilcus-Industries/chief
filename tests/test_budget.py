"""Budget: cycle accounting, thresholds, session refusal/downgrade/notice."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.manager import SessionManager
from chief.agent.tools import ToolRegistry
from chief.budget import Budget, BudgetState
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.base import Completion, TextDelta, Usage

from .fakes import FakeProvider


async def noop_delta(text: str) -> None:
    pass


def costed_turn(text: str, cost: float) -> list[TextDelta | Completion]:
    return [Completion(text=text, usage=Usage(cost=cost))]


def make_budget(engine: AsyncEngine, cap: float) -> Budget:
    return Budget(make_session_factory(engine), cap_usd=cap)


async def test_status_transitions_ok_warn_exhausted(engine: AsyncEngine) -> None:
    budget = make_budget(engine, cap=1.0)
    assert (await budget.status()).state is BudgetState.OK
    await budget.record("t", 0.85)
    assert (await budget.status()).state is BudgetState.WARN
    await budget.record("t", 0.2)
    status = await budget.status()
    assert status.state is BudgetState.EXHAUSTED
    assert status.spent == 1.05


async def test_zero_cap_means_unlimited(engine: AsyncEngine) -> None:
    budget = make_budget(engine, cap=0.0)
    await budget.record("t", 100.0)
    assert (await budget.status()).state is BudgetState.OK


def make_manager(
    provider: FakeProvider,
    store: MessageStore,
    budget: Budget,
    downgrade: str | None = None,
) -> SessionManager:
    return SessionManager(
        provider=provider,
        tools_factory=lambda thread, channel: ToolRegistry(),
        store=store,
        default_model="big-model",
        system_prompt="s",
        max_concurrent=4,
        budget=budget,
        downgrade_model=downgrade,
    )


async def test_exhausted_without_downgrade_refuses_turn(
    engine: AsyncEngine, store: MessageStore
) -> None:
    budget = make_budget(engine, cap=1.0)
    await budget.record("t", 2.0)
    provider = FakeProvider([])
    manager = make_manager(provider, store, budget)
    session = await manager.get_or_create("cli:t", "cli")
    result = await session.run_turn("hi", noop_delta)
    assert "budget exhausted" in result.text
    assert provider.calls == []
    saved = await store.load("cli:t")
    assert saved[0] == {"role": "user", "content": "hi"}
    assert "budget exhausted" in str(saved[1]["content"])


async def test_exhausted_with_downgrade_switches_model(
    engine: AsyncEngine, store: MessageStore
) -> None:
    budget = make_budget(engine, cap=1.0)
    await budget.record("t", 2.0)
    provider = FakeProvider([costed_turn("cheap reply", 0.001)])
    manager = make_manager(provider, store, budget, downgrade="small-model")
    session = await manager.get_or_create("cli:t", "cli")
    result = await session.run_turn("hi", noop_delta)
    assert result.text == "cheap reply"
    assert len(provider.calls) == 1


async def test_crossing_warn_threshold_sets_notice(
    engine: AsyncEngine, store: MessageStore
) -> None:
    budget = make_budget(engine, cap=1.0)
    provider = FakeProvider([costed_turn("pricey", 0.9)])
    manager = make_manager(provider, store, budget)
    session = await manager.get_or_create("cli:t", "cli")
    result = await session.run_turn("hi", noop_delta)
    assert result.notice is not None
    assert "warn" in result.notice


async def test_turn_cost_is_recorded(engine: AsyncEngine, store: MessageStore) -> None:
    budget = make_budget(engine, cap=10.0)
    provider = FakeProvider([costed_turn("ok", 0.25)])
    manager = make_manager(provider, store, budget)
    session = await manager.get_or_create("cli:t", "cli")
    await session.run_turn("hi", noop_delta)
    assert (await budget.status()).spent == 0.25
