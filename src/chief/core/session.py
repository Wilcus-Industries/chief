"""Harness-agnostic session contract the task engine builds every turn against.

This module owns the SDK-neutral vocabulary — :class:`Milestone`, :class:`Final`,
:data:`TurnEvent`, :class:`SessionProto`, and :data:`NO_REPLY` — that the engine
(:class:`~chief.core.tasks.TaskManager`) speaks. The only concrete implementation today
is :class:`~chief.core.copilot_session.CopilotTaskSession` over the GitHub Copilot SDK;
:class:`~chief.core.backend.CopilotBackend` builds it. Keeping the contract here (rather
than beside any one SDK's session) is what let the claude-agent-sdk harness be removed
(#88) without touching the engine.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..adapters.base import Attachment

logger = logging.getLogger("chief.core.session")

#: The reply text surfaced when a turn produced no assistant text at all.
NO_REPLY = "(no reply)"


@dataclass(frozen=True)
class Milestone:
    """A short progress line (e.g. a tool-use start), posted to the task thread."""

    text: str


@dataclass(frozen=True)
class Final:
    """The turn's final assistant text."""

    text: str


TurnEvent = Milestone | Final


class SessionProto(Protocol):
    """The slice of a live session the engine drives (structural).

    The engine (:class:`~chief.core.tasks.TaskManager`) only ever touches a session
    through this contract, so an :class:`~chief.core.backend.CopilotBackend` may build
    any conforming object — :class:`~chief.core.copilot_session.CopilotTaskSession`
    today, a different SDK later (#72/#88).
    """

    session_id: str | None
    #: This turn's SDK cost and latest rate-limit status, captured by the session and
    #: read by the engine after a clean turn to drive the budget (M9 / #84).
    last_cost_usd: float
    last_rate_limit_status: str | None
    #: The actually-served model for this turn (#79/#90). For a routed ``openrouter``
    #: session it is the model the provider actually served; for a Copilot ``auto``
    #: session it is what ``auto`` picked — read for observability (``set_model`` is
    #: untrusted on Copilot quota, so the served model is read here, not assumed).
    last_served_model: str | None
    #: This turn's raw premium-request counts (quota name → cumulative used, #80). The
    #: engine sums these into the premium-request budget currency for a Copilot-quota
    #: turn (#84).
    last_premium_requests: dict[str, int]

    def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str) -> None: ...
    async def aclose(self) -> None: ...


async def close_wedged_session(session: SessionProto, *, timeout: float) -> None:
    """Time-boxed ``aclose`` that reaps the CLI subprocess a cancelled close leaks.

    Both wedged-teardown sites (the watchdog's ``_reset_session`` and
    ``ask_condition``'s finally) deliberately bound ``aclose`` — blocking the consumer
    or the scheduler tick loop indefinitely is strictly worse than an abandoned close.
    But cancelling ``aclose`` mid-``disconnect``/``stop`` skips the SDK's own subprocess
    terminate, orphaning the Copilot CLI (#101). On expiry (or failure) this falls back
    to the session's ``force_close`` — a bounded kill — so orphans never accumulate.
    Never raises (best-effort); external cancellation still propagates. The bound is the
    point: do not remove it.

    ``force_close`` is discovered by ``getattr`` rather than declared on
    :class:`SessionProto` — it is a Copilot-specific escape hatch, and keeping it off
    the contract keeps every non-Copilot test fake valid (they have no reaping to do).
    """
    try:
        async with asyncio.timeout(timeout):
            await session.aclose()
        return
    except Exception:
        logger.warning(
            "session aclose timed out/failed; force-closing", exc_info=True
        )
    force = getattr(session, "force_close", None)
    if force is None:
        # Every session that doesn't offer the hatch: today's non-Copilot test fakes,
        # but equally any future non-Copilot backend, which would silently get no reap
        # here. A backend that spawns a subprocess must implement ``force_close``.
        return
    try:
        await force()
    except Exception:
        logger.debug("force_close failed", exc_info=True)
