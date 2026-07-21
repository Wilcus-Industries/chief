"""Per-thread session: serial turn queue over a persisted transcript.

The system prompt is injected at call time rather than stored, so prompt edits
apply to existing threads on the next turn. The budget is checked before each
turn and recorded after."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from chief.agent.compaction import Compactor
from chief.agent.loop import OnDelta, TurnResult, run_turn
from chief.agent.prompt import read_soul
from chief.agent.restart_gate import RestartGate, _NullGate
from chief.agent.tools import ToolDispatcher
from chief.agent.turn_budget import refuse_over_budget, settle_budget
from chief.budget import Budget, BudgetState
from chief.hooks import (
    HookRegistry,
    TurnContext,
    assemble_system,
    run_post_turn,
    tool_screener,
)
from chief.persistence.store import MessageStore
from chief.provider.base import Provider

logger = logging.getLogger(__name__)


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
        hooks: HookRegistry | None = None,
        hooks_timeout_seconds: float = 10.0,
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
        self._hooks = hooks
        self._hooks_timeout_seconds = hooks_timeout_seconds
        self._session_started = False

    @property
    def busy(self) -> bool:
        """True while a turn is in flight (the lock is held across ``run_turn``)."""
        return self._lock.locked()

    @property
    def lock(self) -> asyncio.Lock:
        """The per-turn lock; a wipe holds it so no turn starts mid-write."""
        return self._lock

    async def run_turn(
        self, user_text: str, on_delta: OnDelta, sender: str = "owner"
    ) -> TurnResult:
        """Queue one user turn; returns once the model finishes its reply.

        ``sender`` (``"owner"``, ``"system"``, or a stranger id) is carried to
        the context hooks so private-data hooks can gate on who speaks. The
        per-thread lock is taken before the global semaphore so queued turns on
        one busy thread can't starve every concurrency slot."""
        async with self._lock:
            async with self._semaphore:
                # New turns wait here while a restart drains; active ones are
                # tracked so it waits for this turn to commit. The restart fires
                # at the outermost boundary, once the reply/cursor are durable.
                await self._gate.enter_turn()
                try:
                    return await self._one_turn(user_text, on_delta, sender)
                finally:
                    self._gate.leave_turn()

    async def _one_turn(
        self, user_text: str, on_delta: OnDelta, sender: str
    ) -> TurnResult:
        model = self.model
        status = await self._budget.status() if self._budget else None
        if status is not None and status.state is BudgetState.EXHAUSTED:
            if self._downgrade_model is None:
                return await refuse_over_budget(self._commit, user_text, status)
            model = self._downgrade_model
        await self._run_compaction()
        system = await self._assemble_system(user_text, sender)
        transcript = [
            {"role": "system", "content": system},
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
            post_tool=tool_screener(self._hooks, self._hooks_timeout_seconds, logger),
        )
        new_messages = [{"role": "user", "content": user_text}, *transcript[baseline:]]
        await self._commit(new_messages)
        if self._hooks is not None:
            await run_post_turn(
                self._hooks.post_turn(), result, new_messages,
                self._hooks_timeout_seconds, logger,
            )
        return await settle_budget(self._budget, self.thread_key, result, status)

    async def _assemble_system(self, user_text: str, sender: str) -> str:
        """The full system prompt, composed fresh each turn (never persisted):
        soul, base prompt + origin note, then each package's <hook> block
        (session_start blocks only on a thread's first turn). The per-turn
        :class:`TurnContext` carries ``self._messages`` before this user turn."""
        first_turn = not self._session_started
        self._session_started = True
        # Base prompt + a note of the origin channel so the agent knows which
        # device it's speaking through; soul and package hooks layer on top.
        base = self._system_prompt
        if self._origin_channel:
            base += (
                f"\n\nYou are talking with the owner over "
                f"the '{self._origin_channel}' channel."
            )
        turn = TurnContext(
            user_text=user_text,
            messages=self._messages,
            sender=sender,
            thread_key=self.thread_key,
            channel=self._origin_channel,
        )
        return await assemble_system(
            base=base,
            soul_reader=self._read_soul,
            hooks=self._hooks,
            turn=turn,
            first_turn=first_turn,
            timeout=self._hooks_timeout_seconds,
            logger=logger,
        )

    async def _run_compaction(self, *, force: bool = False) -> bool:
        """Fold old history into a leading note when the transcript nears the
        window (always, when ``force``); True when it changed the transcript."""
        if self._compactor is None:
            return False
        compacted = await self._compactor.compact(
            self._messages, model=self.model, force=force
        )
        if compacted is None:
            return False
        self._messages = compacted
        await self._store.replace(self.thread_key, compacted)
        return True

    async def compact(self) -> str:
        """Force-compact now (``/compact``, nightly autocompact), bypassing the
        threshold. Refuses mid-turn rather than block the caller."""
        if self._compactor is None:
            return "compaction is not configured"
        if self._lock.locked():
            return "thread is mid-turn — try again shortly"
        async with self._lock:
            before = len(self._messages)
            if not await self._run_compaction(force=True):
                return "nothing to compact"
            return f"compacted {before} messages into {len(self._messages)}"

    async def _commit(self, new_messages: list[dict[str, Any]]) -> None:
        self._messages.extend(new_messages)
        await self._store.append(self.thread_key, new_messages)
