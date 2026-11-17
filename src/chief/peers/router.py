"""PeerRouter: resolve a ``send_msg`` target, apply the scope rules, audit,
and deliver (PRD #308).

Delivery to a session is offered to its running turn through the same inbox
as an owner's mid-turn message (``agent/inbox.py``: open only while a turn
runs, read at its next tool step); a message the turn never reaches is handed
back on close, and an idle receiver gets one either way: a system-sender turn,
like a monitor or cron wake. The wake is a tracked background task, never
awaited inline: inline would stall the sender's tool call for the receiver's
whole turn, and deadlock outright on a message to a thread whose own lock the
sender holds. A receiver keeps its own authority — the header only says who
sent the message — and its turn's output follows the non-owner rule like any
other turn's.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from chief.adapters.base import Message
from chief.agent.inbox import Injected
from chief.agent.manager import SessionManager
from chief.audit import AuditLog
from chief.peers.envelope import PeerMessage, new_id, render, session_label
from chief.peers.loopguard import LOOP_WARNING, PairCounter
from chief.persistence.store import MessageStore
from chief.policy import Audience

logger = logging.getLogger(__name__)

SYSTEM = "system"
SUBAGENT_PREFIX = "sub:"

Wake = Callable[[Message], Coroutine[Any, Any, None]]


async def _consumed() -> None:
    """A peer message needs no hook when its turn takes it: it is persisted
    with the turn, and system-sender text is never re-published to the bus."""


class Run(Protocol):
    """The slice of a live subagent run the router routes by."""

    id: str
    parent_key: str


class Subagents(Protocol):
    """The subagent pool as the router sees it (late-bound: built after)."""

    def get(self, run_id: str) -> Run | None: ...
    def deliver(self, run_id: str, text: str) -> None: ...
    def report_to_parent(
        self, run_id: str, message: PeerMessage, warning: str | None
    ) -> None: ...


class PeerRouter:
    """Routes peer messages between sessions and to/from subagents."""

    def __init__(
        self,
        manager: SessionManager,
        store: MessageStore,
        audit: AuditLog,
        wake: Wake,
        counter: PairCounter,
        audience: Audience | None = None,
    ) -> None:
        self._audience = audience
        self._manager = manager
        self._store = store
        self._audit = audit
        self._wake = wake
        self._counter = counter
        self._tasks: set[asyncio.Task[None]] = set()
        self.pool: Subagents | None = None

    async def send(
        self, sender: str, to: str, text: str, reply_to: str | None = None
    ) -> str:
        """A session's ``send_msg``. Returns the tool result (an ``error: …``
        string on refusal)."""
        if to == sender:
            return "error: you can't send a message to yourself"
        if to.startswith(SUBAGENT_PREFIX):
            pool = self.pool
            run = pool.get(to) if pool is not None else None
            if pool is None or run is None or run.parent_key != sender:
                return f"error: no live subagent of yours named '{to}'"
            message, warning, rendered = self._envelope(sender, to, text, reply_to)
            pool.deliver(to, rendered)
            return self._sent(message, warning)
        channel = await self._store.channel(to)
        if channel is None:
            return (
                f"error: no such session '{to}' — use the thread_key from "
                "`session action=list`"
            )
        untrusted = not await self._owner_thread(sender)
        message, warning, rendered = self._envelope(
            sender, to, text, reply_to, untrusted
        )
        self.deliver(to, channel, rendered)
        return self._sent(message, warning)

    def send_from_subagent(
        self, run_id: str, to: str | None, text: str, reply_to: str | None = None
    ) -> str:
        """A subagent's ``send_msg``: only ever to its parent (the default)."""
        run = self.pool.get(run_id) if self.pool is not None else None
        if run is None or self.pool is None:
            return "error: this subagent is no longer running"
        if to and to != run.parent_key:
            return (
                f"error: a subagent can only message its parent "
                f"('{run.parent_key}'), not '{to}'"
            )
        message = PeerMessage(new_id(), run_id, run.parent_key, text, reply_to)
        warning = self._note(message)
        self.pool.report_to_parent(run_id, message, warning)
        return self._sent(message, warning)

    def deliver(self, to: str, channel: str, rendered: str) -> None:
        """Inject into ``to``'s in-flight turn, else wake it with a new turn.

        Synchronous on purpose: the offer and the inbox's close can't
        interleave on the single event loop, so a message is either taken,
        handed back to :meth:`_wake_later`, or refused and woken now."""
        message = Message(channel, SYSTEM, to, rendered)
        session = self._manager.peek(to)
        item = Injected(rendered, _consumed, lambda: self._wake_later(message))
        if session is None or not session.inject(item):
            self._wake_later(message)

    def _wake_later(self, message: Message) -> None:
        """Wake the receiver as a background task: idle, or its turn ended
        before reaching the message (the inbox hands it back on close)."""
        task = asyncio.get_running_loop().create_task(self._wake(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Cancel wakes still in flight (daemon shutdown)."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _owner_thread(self, thread: str) -> bool:
        """Whether ``thread``'s surface reaches only the owner (fails closed
        on a thread with no row)."""
        if self._audience is None:
            return True
        channel = await self._store.channel(thread)
        return channel is not None and self._audience(channel, thread)

    def _envelope(
        self, sender: str, to: str, text: str, reply_to: str | None,
        untrusted: bool = False,
    ) -> tuple[PeerMessage, str | None, str]:
        message = PeerMessage(new_id(), sender, to, text, reply_to)
        warning = self._note(message)
        label = session_label(sender)
        return message, warning, render(message, label, warning, untrusted)

    def _note(self, message: PeerMessage) -> str | None:
        warn = self._counter.note(message.sender, message.to)
        # Who talked to whom, never what: transcripts hold the content.
        self._audit.record(
            "peer_message", id=message.id, sender=message.sender,
            to=message.to, reply_to=message.reply_to,
        )
        return LOOP_WARNING if warn else None

    @staticmethod
    def _sent(message: PeerMessage, warning: str | None) -> str:
        result = f"sent {message.id} to {message.to}"
        return f"{result}\n{warning}" if warning else result
