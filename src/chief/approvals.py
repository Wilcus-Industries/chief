"""Approval cards: ask the owner on the session's surface, first answer wins.

The dispatcher offers every inbound message here before starting a turn, so
an answer is consumed even while the thread's session is mid-turn (a turn
blocked on this very approval would otherwise deadlock the reply behind the
session lock).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

YES_ANSWERS = frozenset({"yes", "y", "approve", "approved", "ok"})

# Fail closed: an unanswered card denies after this long.
APPROVAL_TIMEOUT_SECONDS = 600.0

SendText = Callable[[str], Awaitable[None]]


class ApprovalBroker:
    """At most one pending approval per thread; unanswered cards deny."""

    def __init__(self, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[bool]] = {}

    def resolve(self, thread_key: str, text: str) -> bool:
        """Try to consume an inbound message as an approval answer.

        Returns True when the message answered a pending card (and so must
        not start a turn). Any answer that isn't a clear yes denies.
        """
        future = self._pending.get(thread_key)
        if future is None or future.done():
            return False
        future.set_result(text.strip().lower() in YES_ANSWERS)
        return True

    async def ask(self, thread_key: str, question: str, send: SendText) -> bool:
        """Render one card and wait for the first answer (or time out to no)."""
        if thread_key in self._pending:
            logger.warning(
                "approval already pending on %s; denying new card", thread_key
            )
            return False
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[thread_key] = future
        try:
            await send(question)
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            return False
        finally:
            del self._pending[thread_key]
