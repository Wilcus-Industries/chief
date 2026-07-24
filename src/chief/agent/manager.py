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
            history = await self._repair_interrupted(
                thread_key, await self._store.load(thread_key)
            )
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

    def peek(self, thread_key: str) -> Session | None:
        """The cached live session for a thread, or ``None`` — never creates
        one. Lets a read-only caller (the web history-tool route) check ``.busy``
        without paying to spin up a session it isn't otherwise touching."""
        return self._sessions.get(thread_key)

    async def _repair_interrupted(
        self, thread_key: str, history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Close out tool calls a crash left dangling mid-dispatch — the last
        assistant tool_calls message (found by role, not tail position: a
        parallel call can crash after only some results land, tailing on a
        tool-role message) gets an error result filled in for every call id
        still missing one. Only runs at session creation, so it can't mistake
        a live in-flight call for one `Session._live_append` left orphaned.
        """
        last_call = None
        for m in history:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_call = m
        if last_call is None:
            return history
        calls = last_call.get("tool_calls") or []
        seen = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
        missing = [call for call in calls if call.get("id") not in seen]
        if not missing:
            return history
        repairs = [
            {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": "error: interrupted before this tool call finished "
                "(process restarted)",
            }
            for call in missing
        ]
        await self._store.append(thread_key, repairs)
        return [*history, *repairs]

    async def create(
        self,
        thread_key: str,
        channel: str,
        stream_policy: dict[str, Any] | None = None,
    ) -> bool:
        """Register a thread without a live session (a monitor/schedule can wake
        it); ``stream_policy`` seeds the override at creation, or updates an
        existing thread's. ``True`` only when a new row was made."""
        created = await self._store.ensure_session(thread_key, channel, stream_policy)
        if not created and stream_policy is not None:
            await self._store.set_stream_policy(thread_key, stream_policy)
        return created

    async def stream_policy(self, thread_key: str) -> dict[str, Any] | None:
        return await self._store.stream_policy(thread_key)

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
        """Force-compact one thread now (``/compact`` + nightly autocompact) on
        the live session so in-memory history folds too (a store-only compaction
        is clobbered by the session's next commit). Refuses an unknown thread."""
        channel = await self._store.channel(thread_key)
        if channel is None:
            return f"no such thread: {thread_key}"
        session = await self.get_or_create(thread_key, channel)
        return await session.compact()

    async def clear(self, thread_key: str) -> bool:
        """Wipe a thread's transcript (keep the row) and drop its cached session
        — dropping the cache is the point, a DB wipe alone is cosmetic while a
        loaded ``Session`` still holds the old history. Mid-turn: see :meth:`_wipe`."""
        return await self._wipe(thread_key, self._store.clear)

    async def delete(self, thread_key: str) -> bool:
        """Delete a thread entirely and drop its cached live session (mid-turn
        handling: see :meth:`_wipe`)."""
        return await self._wipe(thread_key, self._store.delete_session)

    async def _wipe(
        self, thread_key: str, op: Callable[[str], Awaitable[None]]
    ) -> bool:
        """Run a store wipe under the session lock — the single guard for every
        caller (``session`` tool, ``/prune``, web cockpit). Refuses (``False``,
        no write) when a cached session is mid-turn: its in-flight tail would
        commit orphan rows over the wipe. Holding the lock across the wipe shuts
        the delete/clear TOCTOU a plain ``is_busy`` pre-check leaves open."""
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
