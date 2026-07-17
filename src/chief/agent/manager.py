"""Session manager: one Session per thread, N turns concurrent overall."""

import asyncio
from collections.abc import Callable

from chief.agent.compaction import Compactor
from chief.agent.session import RestartGate, Session
from chief.agent.tools import ToolDispatcher
from chief.budget import Budget
from chief.persistence.store import MessageStore
from chief.provider.base import Provider

# Builds the (possibly gated) tool dispatcher for one session's context.
ToolsFactory = Callable[[str, str], ToolDispatcher]


class SessionManager:
    """Creates, caches, and resumes per-thread sessions."""

    def __init__(
        self,
        *,
        provider: Provider,
        tools_factory: ToolsFactory,
        store: MessageStore,
        default_model: str,
        system_prompt: str,
        max_concurrent: int,
        budget: Budget | None = None,
        downgrade_model: str | None = None,
        compactor: Compactor | None = None,
        restart_gate: RestartGate | None = None,
        soul_reader: Callable[[], str] | None = None,
    ) -> None:
        self._provider = provider
        self._tools_factory = tools_factory
        self._store = store
        self._default_model = default_model
        self._system_prompt = system_prompt
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._budget = budget
        self._downgrade_model = downgrade_model
        self._compactor = compactor
        self._restart_gate = restart_gate
        self._soul_reader = soul_reader
        self._sessions: dict[str, Session] = {}
        self._create_lock = asyncio.Lock()

    async def get_or_create(self, thread_key: str, channel: str) -> Session:
        """Return the live session for a thread, resuming history from disk."""
        if session := self._sessions.get(thread_key):
            return session
        async with self._create_lock:
            if session := self._sessions.get(thread_key):
                return session
            await self._store.ensure_session(thread_key, channel)
            history = await self._store.load(thread_key)
            override = await self._store.model_override(thread_key)
            origin = await self._store.channel(thread_key)
            session = Session(
                thread_key=thread_key,
                provider=self._provider,
                tools=self._tools_factory(thread_key, channel),
                store=self._store,
                model=override or self._default_model,
                system_prompt=self._system_prompt,
                history=history,
                turn_semaphore=self._semaphore,
                budget=self._budget,
                downgrade_model=self._downgrade_model,
                compactor=self._compactor,
                restart_gate=self._restart_gate,
                origin_channel=origin,
                soul_reader=self._soul_reader,
            )
            self._sessions[thread_key] = session
            return session

    async def set_model(self, thread_key: str, channel: str, model: str) -> None:
        """Owner per-thread model override: live session + persisted row."""
        session = await self.get_or_create(thread_key, channel)
        session.model = model
        await self._store.set_model_override(thread_key, model)

    async def clear(self, thread_key: str) -> None:
        """Wipe a thread's transcript and drop its cached live session.

        Dropping the cache is the point: the DB wipe alone is cosmetic while a
        loaded ``Session`` still holds the old history in memory.
        """
        await self._store.clear(thread_key)
        self._sessions.pop(thread_key, None)

    async def delete(self, thread_key: str) -> None:
        """Delete a thread entirely and drop its cached live session."""
        await self._store.delete_session(thread_key)
        self._sessions.pop(thread_key, None)
