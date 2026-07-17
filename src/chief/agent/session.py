"""Per-thread session: serial turn queue over a persisted transcript.

The system prompt is injected at call time rather than stored, so prompt
edits apply to existing threads on the next turn. The budget is checked
before each turn and recorded after.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from chief.agent.compaction import Compactor
from chief.agent.loop import OnDelta, TurnResult, run_turn
from chief.agent.tools import ToolDispatcher
from chief.budget import Budget, BudgetState, BudgetStatus
from chief.persistence.store import MessageStore
from chief.provider.base import Provider


class Session:
    """One conversation thread. Turns run strictly one at a time."""

    def __init__(
        self,
        *,
        thread_key: str,
        provider: Provider,
        tools: ToolDispatcher,
        store: MessageStore,
        model: str,
        system_prompt: str,
        history: list[dict[str, Any]],
        turn_semaphore: asyncio.Semaphore,
        budget: Budget | None = None,
        downgrade_model: str | None = None,
        compactor: Compactor | None = None,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        self.thread_key = thread_key
        self.model = model
        self._provider = provider
        self._tools = tools
        self._store = store
        self._system_prompt = system_prompt
        self._messages = history
        self._lock = asyncio.Lock()
        self._semaphore = turn_semaphore
        self._budget = budget
        self._downgrade_model = downgrade_model
        self._compactor = compactor
        self._after_commit = after_commit or (lambda: None)

    async def run_turn(self, user_text: str, on_delta: OnDelta) -> TurnResult:
        """Queue one user turn; returns once the model finishes its reply.

        The per-thread lock is taken before the global semaphore so queued
        turns on one busy thread can't starve every concurrency slot.
        """
        async with self._lock:
            async with self._semaphore:
                return await self._one_turn(user_text, on_delta)

    async def _one_turn(self, user_text: str, on_delta: OnDelta) -> TurnResult:
        model = self.model
        status = await self._budget.status() if self._budget else None
        if status is not None and status.state is BudgetState.EXHAUSTED:
            if self._downgrade_model is None:
                return await self._refuse_over_budget(user_text, status)
            model = self._downgrade_model
        await self._maybe_compact()
        transcript = [
            {"role": "system", "content": self._system_prompt},
            *self._messages,
            {"role": "user", "content": user_text},
        ]
        baseline = len(transcript)
        result = await run_turn(
            provider=self._provider,
            model=model,
            messages=transcript,
            tools=self._tools,
            on_delta=on_delta,
        )
        await self._commit(
            [{"role": "user", "content": user_text}, *transcript[baseline:]]
        )
        settled = await self._settle_budget(result, status)
        # A self-edit tool requests a restart mid-turn; fire it only now, with
        # the transcript already persisted, so the exchange survives os.execv.
        self._after_commit()
        return settled

    async def _maybe_compact(self) -> None:
        """Fold old history into a note when the transcript outgrows the
        window; the persisted transcript is truncated to match."""
        if self._compactor is None:
            return
        compacted = await self._compactor.compact(self._messages)
        if compacted is not None:
            self._messages = compacted
            await self._store.replace(self.thread_key, compacted)

    async def _refuse_over_budget(
        self, user_text: str, status: BudgetStatus
    ) -> TurnResult:
        text = (
            f"budget exhausted (${status.spent:.2f} of ${status.cap:.2f} this "
            "cycle) and no downgrade model is configured; not running this turn"
        )
        await self._commit(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": text},
            ]
        )
        return TurnResult(text=text)

    async def _commit(self, new_messages: list[dict[str, Any]]) -> None:
        self._messages.extend(new_messages)
        await self._store.append(self.thread_key, new_messages)

    async def _settle_budget(
        self, result: TurnResult, before: BudgetStatus | None
    ) -> TurnResult:
        if self._budget is None or before is None:
            return result
        await self._budget.record(self.thread_key, result.usage.cost)
        after = await self._budget.status()
        if after.state is not before.state and after.state is not BudgetState.OK:
            notice = (
                f"[budget {after.state.value}: ${after.spent:.2f} of "
                f"${after.cap:.2f} spent this cycle]"
            )
            return replace(result, notice=notice)
        return result
