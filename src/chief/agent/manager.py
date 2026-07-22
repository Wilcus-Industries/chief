"""Session manager: one Session per thread, N turns concurrent overall."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from chief.agent.compaction import Compactor
from chief.agent.restart_gate import RestartGate
from chief.agent.session import Session
from chief.budget import Budget
from chief.hooks import HookRegistry
from chief.persistence.store import MessageStore
from chief.provider.base import Provider
from chief.tools import ToolDispatcher

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
        hooks: HookRegistry | None = None,
        hooks_timeout_seconds: float = 10.0,
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
        self._hooks = hooks
        self._hooks_timeout_seconds = hooks_timeout_seconds
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
                hooks=self._hooks,
                hooks_timeout_seconds=self._hooks_timeout_seconds,
            )
            self._sessions[thread_key] = session
            return session

    async def create(self, thread_key: str, channel: str) -> None:
        """Register a thread (so a monitor/schedule can wake it) without a live
        session."""
        await self._store.ensure_session(thread_key, channel)

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Every known thread — the agent's own `session` list tool."""
        return await self._store.list_sessions()

    @property
    def default_model(self) -> str:
        """The model a thread runs on with no override — see ``set_model``."""
        return self._default_model

    async def set_model(self, thread_key: str, channel: str, model: str) -> None:
        """Owner per-thread model override: live session + persisted row."""
        session = await self.get_or_create(thread_key, channel)
        session.model = model
        await self._store.set_model_override(thread_key, model)

    async def compact(self, thread_key: str) -> str:
        """Force-compact one thread's transcript now (the ``/compact`` command
        and nightly autocompact). Operates on the live session so the in-memory
        history is folded too — a store-only compaction would be clobbered by
        the cached session's next commit. Refuses to create an unknown thread."""
        channel = await self._store.channel(thread_key)
        if channel is None:
            return f"no such thread: {thread_key}"
        session = await self.get_or_create(thread_key, channel)
        return await session.compact()

    async def clear(self, thread_key: str) -> bool:
        """Wipe a thread's transcript (keep the row) and drop its cached session.

        Returns ``False`` untouched when the thread is mid-turn — see
        :meth:`_wipe`. Dropping the cache is the point: the DB wipe alone is
        cosmetic while a loaded ``Session`` still holds the old history.
        """
        return await self._wipe(thread_key, self._store.clear)

    async def delete(self, thread_key: str) -> bool:
        """Delete a thread entirely and drop its cached live session.

        Returns ``False`` untouched when the thread is mid-turn — see
        :meth:`_wipe`.
        """
        return await self._wipe(thread_key, self._store.delete_session)

    async def _wipe(
        self, thread_key: str, op: Callable[[str], Awaitable[None]]
    ) -> bool:
        """Run a store wipe under the session lock, atomically.

        Refuses (returns ``False``, no store write) when a cached session is
        mid-turn: its in-flight tail would commit orphan rows over the wipe.
        Otherwise the lock is held across the wipe so no turn can start in the
        store-write window — closing the delete/clear TOCTOU that a plain
        ``is_busy`` pre-check leaves open. This is the single guard for every
        caller (the ``session`` tool, ``/prune``, and the web cockpit).
        """
        session = self._sessions.get(thread_key)
        if session is None:
            await op(thread_key)
            return True
        if session.busy:  # sync check; the lock grab below runs before any await
            return False
        async with session.lock:
            await op(thread_key)
        self._sessions.pop(thread_key, None)
        return True
