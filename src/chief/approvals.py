"""Approval cards: ask the owner on the session's surface, first answer wins.

The dispatcher offers every inbound message here before starting a turn, so
an answer is consumed even while the thread's session is mid-turn (a turn
blocked on this very approval would otherwise deadlock the reply behind the
session lock).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class Approval(Enum):
    """The owner's answer to an approval card."""

    DENY = "deny"
    ONCE = "once"  # approve this one call
    ALWAYS = "always"  # approve and stop asking about this tool (#187)


# "always" must be checked before the plain yes-set so it wins.
ALWAYS_ANSWERS = frozenset({"always", "always allow", "a", "aa"})
YES_ANSWERS = frozenset({"yes", "y", "approve", "approved", "ok"})

# Fail closed: an unanswered card denies after this long.
APPROVAL_TIMEOUT_SECONDS = 600.0

SendText = Callable[[str], Awaitable[None]]


def parse_answer(text: str) -> Approval:
    """Map a free-text card answer to an Approval; anything unclear denies."""
    answer = text.strip().lower()
    if answer in ALWAYS_ANSWERS:
        return Approval.ALWAYS
    if answer in YES_ANSWERS:
        return Approval.ONCE
    return Approval.DENY


class ApprovalBroker:
    """At most one pending approval per thread; unanswered cards deny."""

    def __init__(self, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[Approval]] = {}

    def resolve(self, thread_key: str, text: str) -> bool:
        """Try to consume an inbound message as an approval answer.

        Returns True when the message answered a pending card (and so must
        not start a turn). The answer's verdict (deny/once/always) is passed
        to the waiting card; anything that isn't a clear yes/always denies.
        """
        future = self._pending.get(thread_key)
        if future is None or future.done():
            return False
        future.set_result(parse_answer(text))
        return True

    async def ask(
        self, thread_key: str, question: str, send: SendText
    ) -> Approval:
        """Render one card and wait for the first answer (or time out to deny)."""
        if thread_key in self._pending:
            logger.warning(
                "approval already pending on %s; denying new card", thread_key
            )
            return Approval.DENY
        future: asyncio.Future[Approval] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[thread_key] = future
        try:
            await send(question)
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            return Approval.DENY
        finally:
            del self._pending[thread_key]
