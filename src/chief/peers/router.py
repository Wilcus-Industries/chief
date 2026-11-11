"""PeerRouter: resolve a ``send_msg`` target, apply the scope rules, audit,
and deliver (PRD #308).

Delivery to a session goes through the live session's inbox when it is
mid-turn (drained at its next tool step), else wakes it with a system-sender
turn like a monitor or cron wake. The wake is a tracked background task, never
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
from chief.agent.manager import SessionManager
from chief.audit import AuditLog
from chief.peers.envelope import PeerMessage, new_id, render, session_label
from chief.peers.loopguard import LOOP_WARNING, PairCounter
from chief.persistence.store import MessageStore

logger = logging.getLogger(__name__)

SYSTEM = "system"
SUBAGENT_PREFIX = "sub:"

Wake = Callable[[Message], Coroutine[Any, Any, None]]


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
    ) -> None:
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
        message, warning, rendered = self._envelope(sender, to, text, reply_to)
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

        Synchronous on purpose: the busy check and the inbox append can't
        interleave with the session's lock release on the single event loop."""
        session = self._manager.peek(to)
        if session is not None and session.deliver(rendered):
            return
        task = asyncio.get_running_loop().create_task(
            self._wake(Message(channel, SYSTEM, to, rendered))
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Cancel wakes still in flight (daemon shutdown)."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _envelope(
        self, sender: str, to: str, text: str, reply_to: str | None
    ) -> tuple[PeerMessage, str | None, str]:
        message = PeerMessage(new_id(), sender, to, text, reply_to)
        warning = self._note(message)
        return message, warning, render(message, session_label(sender), warning)

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
