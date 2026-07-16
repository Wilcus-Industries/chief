"""Session manager: one Session per thread, N turns concurrent overall."""

import asyncio

from chief.agent.session import Session
from chief.agent.tools import ToolRegistry
from chief.persistence.store import MessageStore
from chief.provider.base import Provider


class SessionManager:
    """Creates, caches, and resumes per-thread sessions."""

    def __init__(
        self,
        *,
        provider: Provider,
        registry: ToolRegistry,
        store: MessageStore,
        default_model: str,
        system_prompt: str,
        max_concurrent: int,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._store = store
        self._default_model = default_model
        self._system_prompt = system_prompt
        self._semaphore = asyncio.Semaphore(max_concurrent)
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
            session = Session(
                thread_key=thread_key,
                provider=self._provider,
                registry=self._registry,
                store=self._store,
                model=self._default_model,
                system_prompt=self._system_prompt,
                history=history,
                turn_semaphore=self._semaphore,
            )
            self._sessions[thread_key] = session
            return session
