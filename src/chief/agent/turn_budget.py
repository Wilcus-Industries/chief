"""Budget checks around a turn: the over-budget refusal and the post-turn settle.

Kept out of :mod:`chief.agent.session` so the session stays under the file-length
cap; both are pure functions over just the pieces they need.
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from chief.agent.loop import TurnResult
from chief.budget import Budget, BudgetState, BudgetStatus

Commit = Callable[[list[dict[str, Any]]], Awaitable[None]]


async def refuse_over_budget(
    commit: Commit, user_text: str, status: BudgetStatus
) -> TurnResult:
    """Record and return the refusal when the budget is spent and no downgrade
    model is configured — the turn never reaches the provider."""
    text = (
        f"budget exhausted (${status.spent:.2f} of ${status.cap:.2f} this "
        "cycle) and no downgrade model is configured; not running this turn"
    )
    await commit(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": text},
        ]
    )
    return TurnResult(text=text)


async def settle_budget(
    budget: Budget | None,
    thread_key: str,
    result: TurnResult,
    before: BudgetStatus | None,
) -> TurnResult:
    """Record the turn's cost and attach a threshold-crossing notice, if any."""
    if budget is None or before is None:
        return result
    await budget.record(thread_key, result.usage.cost)
    after = await budget.status()
    if after.state is not before.state and after.state is not BudgetState.OK:
        notice = (
            f"[budget {after.state.value}: ${after.spent:.2f} of "
            f"${after.cap:.2f} spent this cycle]"
        )
        return replace(result, notice=notice)
    return result
