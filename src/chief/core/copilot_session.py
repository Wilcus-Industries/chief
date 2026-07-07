"""Persistent per-task Copilot-SDK session (issue #76, part of #72).

The Claude twin of this module is :mod:`chief.core.session`; this one drives the
**GitHub Copilot SDK** (import name ``copilot``) behind the same
:class:`~chief.core.session.SessionProto` the engine builds every session against. The
central mechanism is a *real* Copilot-SDK turn: create a streaming session and run one
owner turn to a reply on a Copilot-quota model.

**Event-model rewrite.** claude-agent-sdk hands the session a linear async stream of
typed messages; the Copilot SDK instead delivers a firehose of 40+ event types through a
**callback** (``session.on(handler)``) and signals turn completion out-of-band. So this
module bridges that push model onto ``run_turn``'s pull model — a registered handler
pumps every :class:`SessionEvent` onto an :class:`asyncio.Queue`, and ``run_turn``
drains the queue, mapping the handful of events chief cares about onto ``Milestone`` /
``Final`` and re-deriving the turn boundary:

* ``assistant.message`` (:class:`AssistantMessageData`) → one ``Final`` per message, so
  the engine surfaces each block to the owner individually (per-block streaming, #64) —
  the analogue of claude-agent-sdk's ``TextBlock`` → ``Final``.
* ``tool.execution_start`` (:class:`ToolExecutionStartData`) → ``Milestone("using …")``,
  the analogue of ``ToolUseBlock`` → ``Milestone``.
* ``assistant.usage`` (:class:`AssistantUsageData`) → ``last_cost_usd`` (summed across
  the turn's model calls; ``0.0`` on Copilot quota, which reports no dollar cost).
* ``session.limits_exhausted`` → ``last_rate_limit_status = "rejected"`` so the budget
  gate can back off — Copilot's quota model has no per-turn allowed/warning status like
  claude-agent-sdk's ``RateLimitEvent``, so ``rejected`` is the only synthesised signal.
* ``session.idle`` (:class:`SessionIdleData`) is the turn boundary — **not**
  ``assistant.turn_end``, which fires once per inner tool round-trip (#72 spike).
* ``session.error`` / ``model.call_failure`` → raise, aborting the turn.

This slice wires only what one owner text turn needs (model, resume, cwd). Tools, the
permission gate, and the persona are accepted at the backend seam but not yet
forwarded — later #72 slices map them onto the SDK's permission callback, hooks, and
custom tools.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

from copilot import CopilotClient
from copilot.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    ModelCallFailureData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
    SessionLimitsExhaustedRequestedData,
    ToolExecutionStartData,
)

from ..adapters.base import Attachment
from .agent import NO_REPLY
from .session import Final, Milestone, TurnEvent

logger = logging.getLogger("chief.core.copilot_session")


class CopilotTurnError(RuntimeError):
    """A Copilot turn ended on a session error / model-call failure event."""


class _CopilotSession(Protocol):
    """The slice of ``copilot.CopilotSession`` this module uses (structural)."""

    session_id: str

    def on(
        self, handler: Callable[[SessionEvent], None]
    ) -> Callable[[], None]: ...
    async def send(self, prompt: str, *, attachments: Any = ...) -> str: ...
    async def set_model(self, model: str) -> None: ...
    async def abort(self) -> None: ...
    async def disconnect(self) -> None: ...


class _CopilotClient(Protocol):
    """The slice of ``copilot.CopilotClient`` this module uses (structural).

    The third-party boundary: production spawns the real runtime, tests inject a fake so
    a real turn can be dispatched through the session without a subprocess.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create_session(
        self,
        *,
        model: str | None = ...,
        working_directory: str | None = ...,
    ) -> _CopilotSession: ...
    async def resume_session(
        self,
        session_id: str,
        *,
        model: str | None = ...,
        working_directory: str | None = ...,
    ) -> _CopilotSession: ...


CopilotClientFactory = Callable[[], _CopilotClient]


def _default_copilot_client() -> _CopilotClient:
    return CopilotClient()


def _describe_error(data: SessionErrorData | ModelCallFailureData) -> str:
    """A one-line reason from an error / failure event, for the raised exception."""
    if isinstance(data, SessionErrorData):
        return f"{data.error_type}: {data.message}"
    reason = data.error_message or data.error_type or "model call failed"
    return f"{data.source}: {reason}"


class CopilotTaskSession:
    """One task's live Copilot-SDK conversation, exposed as a ``SessionProto``.

    Lazily connected: construction is cheap (mirrors :class:`TaskSession`), and the
    first ``run_turn`` spins up the client, opens the session, and registers the event
    handler.
    """

    def __init__(
        self,
        *,
        model: str,
        resume: str | None = None,
        cwd: str | None = None,
        client_factory: CopilotClientFactory = _default_copilot_client,
    ) -> None:
        self._model = model
        self._resume = resume
        self._cwd = cwd
        self._client_factory = client_factory
        self._client: _CopilotClient | None = None
        self._session: _CopilotSession | None = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Per-turn event sink; the ``on`` handler pumps events here and ``run_turn``
        #: drains it. Recreated each turn so a stray connect-time event can't bleed in.
        self._queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        #: Resumable session id, captured from the live session on connect.
        self.session_id: str | None = resume
        #: This turn's summed SDK cost; reset each turn (mirrors :class:`TaskSession`).
        self.last_cost_usd: float = 0.0
        #: Latest synthesised rate-limit status — only ``rejected`` (on quota
        #: exhaustion); Copilot exposes no allowed/warning status. Sticky across turns.
        self.last_rate_limit_status: str | None = None

    def _on_event(self, event: SessionEvent) -> None:
        """Pump one SDK event onto the current turn's queue (thread-safe).

        The SDK may dispatch handlers off the event loop, so hop back onto it with
        ``call_soon_threadsafe`` — safe whether the callback fires on- or off-loop.
        """
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        self._loop = asyncio.get_running_loop()
        client = self._client_factory()
        await client.start()
        if self._resume is not None:
            session = await client.resume_session(
                self._resume, model=self._model, working_directory=self._cwd
            )
        else:
            session = await client.create_session(
                model=self._model, working_directory=self._cwd
            )
        session.on(self._on_event)
        self._client = client
        self._session = session
        self.session_id = session.session_id
        self._connected = True

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        """Send ``text``, stream the response as ``Milestone`` / ``Final`` events.

        Drains the event queue until ``session.idle`` (the turn boundary), mapping each
        event chief cares about. A turn that streams no assistant text still yields one
        ``Final(NO_REPLY)`` so callers never see an empty stream (mirrors
        :class:`TaskSession`).
        """
        await self._ensure_connected()
        assert self._session is not None  # set by _ensure_connected
        if attachments:
            # This slice is text-only; attachment/image forwarding is a later #72 slice
            # (the SDK's send() takes attachments, PDFs are pre-extracted upstream).
            logger.warning(
                "CopilotTaskSession dropped %d attachment(s) — not wired yet (#72)",
                len(attachments),
            )
        self._queue = asyncio.Queue()
        self.last_cost_usd = 0.0  # this turn's spend only; the engine sums per turn
        text_blocks_seen = 0
        await self._session.send(text)
        while True:
            data = (await self._queue.get()).data
            if isinstance(data, SessionIdleData):
                break
            if isinstance(data, AssistantMessageData):
                if data.content:
                    text_blocks_seen += 1
                    yield Final(text=data.content)
            elif isinstance(data, ToolExecutionStartData):
                yield Milestone(text=f"using {data.tool_name}")
            elif isinstance(data, AssistantUsageData):
                if data.cost is not None:
                    self.last_cost_usd += data.cost
            elif isinstance(data, SessionLimitsExhaustedRequestedData):
                self.last_rate_limit_status = "rejected"
            elif isinstance(data, (SessionErrorData, ModelCallFailureData)):
                raise CopilotTurnError(_describe_error(data))
        if text_blocks_seen == 0:
            yield Final(text=NO_REPLY)

    async def interrupt(self) -> None:
        """Abort the in-flight turn (steering 'stop / do X instead')."""
        if self._session is not None:
            await self._session.abort()

    async def set_model(self, model: str) -> None:
        """Switch the model — live if connected, else for the next connect."""
        self._model = model
        if self._session is not None:
            await self._session.set_model(model)

    async def aclose(self) -> None:
        """Disconnect the session and stop the client, freeing the runtime."""
        if self._session is not None:
            await self._session.disconnect()
            self._session = None
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._connected = False
