"""Per-thread session: serial turn queue over a persisted transcript.

The system prompt is injected at call time rather than stored, so prompt
edits apply to existing threads on the next turn.
"""

import asyncio
from typing import Any

from chief.agent.loop import OnDelta, TurnResult, run_turn
from chief.agent.tools import ToolRegistry
from chief.persistence.store import MessageStore
from chief.provider.base import Provider


class Session:
    """One conversation thread. Turns run strictly one at a time."""

    def __init__(
        self,
        *,
        thread_key: str,
        provider: Provider,
        registry: ToolRegistry,
        store: MessageStore,
        model: str,
        system_prompt: str,
        history: list[dict[str, Any]],
        turn_semaphore: asyncio.Semaphore,
    ) -> None:
        self.thread_key = thread_key
        self.model = model
        self._provider = provider
        self._registry = registry
        self._store = store
        self._system_prompt = system_prompt
        self._messages = history
        self._lock = asyncio.Lock()
        self._semaphore = turn_semaphore

    async def run_turn(self, user_text: str, on_delta: OnDelta) -> TurnResult:
        """Queue one user turn; returns once the model finishes its reply.

        The per-thread lock is taken before the global semaphore so queued
        turns on one busy thread can't starve every concurrency slot.
        """
        async with self._lock:
            async with self._semaphore:
                transcript = [
                    {"role": "system", "content": self._system_prompt},
                    *self._messages,
                    {"role": "user", "content": user_text},
                ]
                baseline = len(transcript)
                result = await run_turn(
                    provider=self._provider,
                    model=self.model,
                    messages=transcript,
                    registry=self._registry,
                    on_delta=on_delta,
                )
                new_messages = [
                    {"role": "user", "content": user_text},
                    *transcript[baseline:],
                ]
                self._messages.extend(new_messages)
                await self._store.append(self.thread_key, new_messages)
                return result
