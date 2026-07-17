"""Per-thread session: serial turn queue over a persisted transcript.

The system prompt is injected at call time rather than stored, so prompt
edits apply to existing threads on the next turn. The budget is checked
before each turn and recorded after.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from chief.agent.compaction import Compactor
from chief.agent.loop import OnDelta, TurnResult, run_turn
from chief.agent.prompt import read_soul
from chief.agent.tools import ToolDispatcher
from chief.budget import Budget, BudgetState, BudgetStatus
from chief.persistence.store import MessageStore
from chief.provider.base import Provider


class RestartGate(Protocol):
    """Brackets a turn so a pending self-edit restart drains it before exec.

    ``enter_turn`` blocks while a restart is pending and otherwise registers
    the turn as active; ``leave_turn`` deregisters it; ``fire_if_requested``
    restarts (never returning) once the turn has committed. See
    ``chief.selfedit.recovery.RestartController``.
    """

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...


class _NullGate:
    """No-op gate for sessions with no restart coordinator (tests, non-daemon)."""

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...


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
        restart_gate: RestartGate | None = None,
        origin_channel: str | None = None,
        soul_reader: Callable[[], str] | None = None,
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
        self._gate: RestartGate = restart_gate or _NullGate()
        self._origin_channel = origin_channel
        self._read_soul = soul_reader or read_soul

    @property
    def busy(self) -> bool:
        """True while a turn is in flight (the per-thread lock is held for the
        whole of ``run_turn``)."""
        return self._lock.locked()

    async def run_turn(self, user_text: str, on_delta: OnDelta) -> TurnResult:
        """Queue one user turn; returns once the model finishes its reply.

        The per-thread lock is taken before the global semaphore so queued
        turns on one busy thread can't starve every concurrency slot.
        """
        async with self._lock:
            async with self._semaphore:
                # Held new turns wait here while a restart drains; active ones
                # are tracked so the restart waits for this turn to commit. The
                # restart itself fires at the outermost boundary (dispatcher /
                # imessage poll) once the reply — and the inbound cursor — are
                # durable too, never here where the reply is still un-sent.
                await self._gate.enter_turn()
                try:
                    return await self._one_turn(user_text, on_delta)
                finally:
                    self._gate.leave_turn()

    async def _one_turn(self, user_text: str, on_delta: OnDelta) -> TurnResult:
        model = self.model
        status = await self._budget.status() if self._budget else None
        if status is not None and status.state is BudgetState.EXHAUSTED:
            if self._downgrade_model is None:
                return await self._refuse_over_budget(user_text, status)
            model = self._downgrade_model
        await self._maybe_compact()
        transcript = [
            {"role": "system", "content": self._system_content()},
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
        return await self._settle_budget(result, status)

    def _system_content(self) -> str:
        """The full system prompt, assembled at call time (never persisted): the
        owner's soul at the top (read fresh each turn, so soul edits apply
        immediately), then the base prompt, then a note of the thread's origin
        channel so the agent knows which device it's speaking through."""
        prompt = self._system_prompt
        if soul := self._read_soul():
            prompt = f"{soul}\n\n{prompt}"
        if self._origin_channel:
            prompt = (
                f"{prompt}\n\nYou are talking with the owner over "
                f"the '{self._origin_channel}' channel."
            )
        return prompt

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
