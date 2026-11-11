"""Per-thread session: serial turn queue over a persisted transcript.

styleguide: file-length — one cohesive Session class (turn queue, budget,
compaction, prompt assembly); splitting it would scatter one lifecycle.

The system prompt is injected at call time rather than stored, so prompt
edits apply to existing threads on the next turn. The budget is checked
before each turn and recorded after.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from chief.agent.compaction import Compactor
from chief.agent.inbox import Inbox, Injected, OnInterim
from chief.agent.loop import OnTool, OnToolResult, TurnResult, run_turn
from chief.agent.model_call import OnDelta
from chief.agent.prompt import read_soul
from chief.agent.restart_gate import RestartGate, TurnTicket, _NullGate
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
from chief.provider.base import Provider, for_model
from chief.tools import ToolDispatcher

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
        self._inbox = Inbox()

    @property
    def busy(self) -> bool:
        """True while a turn is in flight (the per-thread lock is held for the
        whole of ``run_turn``)."""
        return self._lock.locked()

    @property
    def lock(self) -> asyncio.Lock:
        """The per-turn lock; a wipe holds it so no turn starts mid-write."""
        return self._lock

    def inject(self, item: Injected) -> bool:
        """Offer an owner message to the in-flight turn (seen at its next tool
        boundary, or handed back via ``item.on_leftover``); False when idle."""
        return self._inbox.offer(item)

    async def run_turn(
        self,
        user_text: str,
        on_delta: OnDelta,
        sender: str = "owner",
        on_tool: OnTool | None = None,
        on_tool_result: OnToolResult | None = None,
        on_begin: Callable[[], Awaitable[None]] | None = None,
        on_interim: OnInterim | None = None,
        on_reasoning: OnDelta | None = None,
    ) -> TurnResult:
        """Queue one user turn; returns once the model finishes its reply.

        ``on_begin`` fires once the turn holds the thread — the moment its user
        text becomes the conversation's next message, not when it was queued.
        ``on_interim`` delivers an answer an injected message overtook (see
        :func:`chief.agent.loop.run_turn`); ``on_reasoning`` streams the
        model's thinking, display only.

        ``sender`` (``"owner"``, ``"system"``, or a stranger id) is carried to
        the context hooks so private-data hooks can gate on who is speaking;
        it defaults to the owner. The per-thread lock is taken before the
        global semaphore so queued turns on one busy thread can't starve every
        concurrency slot.

        The turn is recorded in flight before it queues (``track``) and
        dropped when it returns or raises; a cancel (the daemon going down)
        leaves it for the next boot to replay or wake.
        """
        ticket = self._gate.track(
            self.thread_key, self._origin_channel or "", sender, user_text
        )
        try:
            result = await self._queued_turn(
                ticket, user_text, on_delta, sender, on_tool, on_tool_result,
                on_begin, on_interim, on_reasoning,
            )
        except Exception:
            ticket.done()
            raise
        ticket.done()
        return result

    async def _queued_turn(
        self,
        ticket: TurnTicket,
        user_text: str,
        on_delta: OnDelta,
        sender: str,
        on_tool: OnTool | None,
        on_tool_result: OnToolResult | None,
        on_begin: Callable[[], Awaitable[None]] | None,
        on_interim: OnInterim | None,
        on_reasoning: OnDelta | None,
    ) -> TurnResult:
        async with self._lock:
            async with self._semaphore:
                # Held new turns wait here while a restart drains; active ones
                # are tracked so the restart waits for this turn to commit. The
                # restart itself fires at the outermost boundary (dispatcher /
                # imessage poll) once the reply — and the inbound cursor — are
                # durable too, never here where the reply is still un-sent.
                await self._gate.enter_turn()
                self._inbox.open()
                try:
                    if on_begin is not None:
                        await on_begin()
                    return await self._one_turn(
                        user_text, on_delta, sender, on_tool, on_tool_result,
                        on_interim, on_reasoning, ticket,
                    )
                finally:
                    try:
                        self._inbox.close()
                    finally:
                        self._gate.leave_turn()

    async def _one_turn(
        self,
        user_text: str,
        on_delta: OnDelta,
        sender: str,
        on_tool: OnTool | None = None,
        on_tool_result: OnToolResult | None = None,
        on_interim: OnInterim | None = None,
        on_reasoning: OnDelta | None = None,
        ticket: TurnTicket | None = None,
    ) -> TurnResult:
        model = self.model
        status = await self._budget.status() if self._budget else None
        if status is not None and status.state is BudgetState.EXHAUSTED:
            if self._downgrade_model is None:
                return await refuse_over_budget(self._commit, user_text, status)
            model = self._downgrade_model
        await self._maybe_compact()
        system = await self._assemble_system(user_text, sender)
        # The user turn is persisted up front — before the model even starts —
        # so it's never lost to a mid-turn crash; the assistant/tool messages
        # the loop produces persist as they land (`_live_append`), not batched
        # at the end, so a thread viewed mid-turn shows real progress instead
        # of nothing (#261's pending tool-call state depends on this).
        user_message = {"role": "user", "content": user_text}
        await self._store.append(self.thread_key, [user_message])
        if ticket is not None:
            ticket.started()
        transcript = [
            {"role": "system", "content": system},
            *self._messages,
            user_message,
        ]
        baseline = len(transcript)
        try:
            result = await run_turn(
                provider=self._provider,
                model=model,
                messages=transcript,
                tools=self._tools,
                on_delta=on_delta,
                post_tool=tool_screener(
                    self._hooks, self._hooks_timeout_seconds, logger
                ),
                on_commit=self._live_append,
                on_tool=on_tool,
                on_tool_result=on_tool_result,
                injections=self._inbox,
                on_interim=on_interim,
                on_reasoning=on_reasoning,
            )
        except BaseException:
            # A provider/network raise mid-turn must not orphan the turn: the
            # store already has the user message and whatever `_live_append`
            # committed (`transcript` is mutated in place by `run_turn`), so
            # in-memory history has to catch up to the same point before the
            # exception propagates — otherwise the next turn diverges from
            # what's on disk.
            self._messages.extend([user_message, *transcript[baseline:]])
            raise
        new_messages = [user_message, *transcript[baseline:]]
        self._messages.extend(new_messages)
        if self._hooks is not None:
            await run_post_turn(
                self._hooks.post_turn(), result, for_model(new_messages),
                self._hooks_timeout_seconds, logger,
            )
        return await settle_budget(self._budget, self.thread_key, result, status)

    async def _assemble_system(self, user_text: str, sender: str) -> str:
        """The full system prompt, composed fresh each turn (never persisted):
        the soul on top, then the base prompt + origin note, then each installed
        package's context contribution as a name-sorted <hook> block. session_
        start blocks are added only on a thread's first turn of the process.

        The per-turn :class:`TurnContext` handed to the context hooks carries
        ``self._messages`` as-is — the transcript *before* this user turn is
        appended — so a hook judges relevance against the conversation so far."""
        first_turn = not self._session_started
        self._session_started = True
        turn = TurnContext(
            user_text=user_text,
            messages=for_model(self._messages),
            sender=sender,
            thread_key=self.thread_key,
            channel=self._origin_channel,
        )
        return await assemble_system(
            base=self._base_system(),
            soul_reader=self._read_soul,
            hooks=self._hooks,
            turn=turn,
            first_turn=first_turn,
            timeout=self._hooks_timeout_seconds,
            logger=logger,
        )

    def _base_system(self) -> str:
        """The base prompt plus a note of the thread's origin channel so the
        agent knows which device it's speaking through. Soul and package hooks
        layer on top in :meth:`_assemble_system`."""
        prompt = self._system_prompt
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
        compacted = await self._compactor.compact(self._messages, model=self.model)
        if compacted is not None:
            self._messages = compacted
            await self._store.replace(self.thread_key, compacted)

    async def compact(self) -> str:
        """Force a compaction now, bypassing the threshold (the ``/compact``
        command and nightly autocompact). Refuses mid-turn rather than block on
        the turn lock, so a caller never hangs behind a long reply."""
        if self._compactor is None:
            return "compaction is not configured"
        if self._lock.locked():
            return "thread is mid-turn — try again in a moment"
        async with self._lock:
            before = len(self._messages)
            compacted = await self._compactor.compact(
                self._messages, model=self.model, force=True
            )
            if compacted is None:
                return "nothing to compact"
            self._messages = compacted
            await self._store.replace(self.thread_key, compacted)
            return f"compacted {before} messages into {len(compacted)}"

    async def _commit(self, new_messages: list[dict[str, Any]]) -> None:
        self._messages.extend(new_messages)
        await self._store.append(self.thread_key, new_messages)

    async def _live_append(self, message: dict[str, Any]) -> None:
        """Persist one turn message (assistant call or tool result) the
        instant the loop produces it. ``self._messages`` is extended once, in
        ``_one_turn``, after the whole turn finishes — this only writes the
        store early, so a concurrent reader sees real progress mid-turn."""
        await self._store.append(self.thread_key, [message])
