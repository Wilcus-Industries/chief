"""Persistent per-task SDK session.

Wraps a :class:`ClaudeSDKClient` kept open in streaming-input mode: one session per
task (DESIGN: task execution engine). ``run_turn`` feeds a message and streams the
response as :class:`Milestone` / :class:`Final` events; ``interrupt`` aborts the current
turn (steering); ``aclose`` frees the subprocess on archive. The resumable
``session_id`` is captured from the stream so a restart can resume via
``ClaudeAgentOptions(resume=…)``.

M2 wires **no tools** — owner sessions are plain chat — so ``Milestone`` events (from
tool-use) rarely fire yet; the machinery lights up when the gate + tools land (M3+).
"""

import base64
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookEvent, McpServerConfig, SdkPluginConfig

from ..adapters.base import Attachment
from .agent import NO_REPLY

logger = logging.getLogger("chief.core.session")


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
    """The slice of :class:`TaskSession` the engine drives (structural).

    The engine (:class:`~chief.core.tasks.TaskManager`) only ever touches a session
    through this contract, so an :class:`~chief.core.backend.AgentBackend` may build any
    conforming object — :class:`TaskSession` over claude-agent-sdk today, a different
    SDK later (#72).
    """

    session_id: str | None
    #: This turn's SDK cost and latest rate-limit status, captured by the session and
    #: read by the engine after a clean turn to drive the budget (M9).
    last_cost_usd: float
    last_rate_limit_status: str | None
    #: The actually-served model for this turn (#79/#90). For a routed ``openrouter``
    #: session it is the model the provider actually served; for a Copilot ``auto``
    #: session it is what ``auto`` picked — read for observability (``set_model`` is
    #: untrusted on Copilot quota, so the served model is read here, not assumed).
    last_served_model: str | None

    def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str) -> None: ...
    async def aclose(self) -> None: ...


class _Client(Protocol):
    """The slice of :class:`ClaudeSDKClient` the session uses (structural)."""

    async def connect(self) -> None: ...
    async def query(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        session_id: str = ...,
    ) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str | None = ...) -> None: ...
    async def disconnect(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], _Client]


def _default_client(options: ClaudeAgentOptions) -> _Client:
    return ClaudeSDKClient(options)


def _build_user_message(
    text: str, attachments: Sequence[Attachment]
) -> dict[str, Any]:
    """The streaming user envelope carrying media as content blocks (Anthropic shape).

    Mirrors the SDK's string path (``client.py``: ``type=user`` /
    ``parent_tool_use_id=None`` / ``session_id="default"``) but with ``content`` as a
    block list: a leading ``text`` block, then a ``document`` block per PDF and an
    ``image`` block per image, each base64-encoded. The text block is kept even when
    empty so a media-only turn (a bare photo) still has a prompt slot.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for att in attachments:
        block_type = "document" if att.media_type == "application/pdf" else "image"
        content.append(
            {
                "type": block_type,
                "source": {
                    "type": "base64",
                    "media_type": att.media_type,
                    "data": base64.b64encode(att.data).decode("ascii"),
                },
            }
        )
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


class TaskSession:
    """One task's live SDK conversation."""

    def __init__(
        self,
        *,
        model: str,
        resume: str | None = None,
        fork_session: bool = False,
        can_use_tool: CanUseTool | None = None,
        hooks: dict[HookEvent, list[HookMatcher]] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict[str, McpServerConfig] | None = None,
        plugins: list[SdkPluginConfig] | None = None,
        skills: list[str] | None = None,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self._options = ClaudeAgentOptions(
            model=model,
            resume=resume,
            fork_session=fork_session,
            can_use_tool=can_use_tool,
            hooks=hooks,
            system_prompt=system_prompt,
            cwd=cwd,
            allowed_tools=allowed_tools if allowed_tools is not None else [],
            disallowed_tools=disallowed_tools if disallowed_tools is not None else [],
            mcp_servers=mcp_servers if mcp_servers is not None else {},
            # Packaged skills (M10): a local plugin manifest provides them, the skills=
            # filter scopes which are on. Owner-only — guests pass neither, so no skill
            # is ever discovered for them. skills=None keeps the SDK default behavior.
            plugins=plugins if plugins is not None else [],
            skills=skills,
        )
        self._client_factory = client_factory
        self._client = client_factory(self._options)
        self._connected = False
        #: Resumable SDK session id, populated from the first turn's stream.
        self.session_id: str | None = resume
        #: Last turn's SDK cost (``ResultMessage.total_cost_usd``); reset each turn so
        #: the engine can roll exactly this turn's spend into the monthly total (M9).
        self.last_cost_usd: float = 0.0
        #: Latest ``RateLimitEvent`` status seen (``allowed``/``allowed_warning``/
        #: ``rejected``); ``rejected`` lets the budget gate back off (M9). Sticky across
        #: turns — the CLI only re-emits on a transition.
        self.last_rate_limit_status: str | None = None
        #: The actually-served model, captured from each ``AssistantMessage.model`` this
        #: turn (#79/#90); reset each turn. Read for observability by the engine.
        self.last_served_model: str | None = None

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        """Send ``text`` (+ any media), stream the response as milestone/final events.

        A dead/expired resume id makes the CLI exit non-zero on the turn's first
        message. If nothing has streamed yet and we were resuming, drop the resume and
        retry once on a fresh session rather than crashing the whole turn — the new id
        is then persisted by the engine, healing the stale pointer. (A genuine
        first-message ``ProcessError`` on a resuming turn also restarts fresh; an
        acceptable degradation vs. a hard crash.)
        """
        streamed = False
        try:
            async for event in self._stream_once(text, attachments):
                streamed = True
                yield event
        except ProcessError as exc:
            if streamed or self._options.resume is None:
                raise
            # Log exit/stderr so a benign dead-resume self-heal is distinguishable
            # from a swallowed real fault (fresh session orphans the prior transcript).
            logger.warning(
                "resume failed (exit %s); retrying on a fresh session. stderr: %s",
                exc.exit_code,
                exc.stderr,
            )
            await self._reset_to_fresh()
            async for event in self._stream_once(text, attachments):
                yield event

    async def _stream_once(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        """One streamed turn against the current client (no resume fallback).

        No media → the SDK's plain ``str`` path. With media → a one-item async-iterable
        of the user envelope (the only way to carry content blocks into ``query``).

        Each ``TextBlock`` in the stream yields its own ``Final`` event as it arrives,
        so the engine can surface each block to the owner individually (per-block reply
        streaming, issue #64). A turn that produces no text block still yields exactly
        one ``Final(text=NO_REPLY)`` so callers never see an empty stream.
        """
        await self._ensure_connected()
        await self._client.query(self._prompt(text, attachments))
        self.last_cost_usd = 0.0  # this turn's spend only; the engine sums per turn
        self.last_served_model = None  # this turn's served model; reset each turn
        text_blocks_seen = 0
        async for message in self._client.receive_response():
            session_id = getattr(message, "session_id", None)
            if session_id:
                self.session_id = session_id
            if isinstance(message, AssistantMessage):
                self.last_served_model = message.model
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_blocks_seen += 1
                        yield Final(text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield Milestone(text=f"using {block.name}")
            elif isinstance(message, ResultMessage):
                self.last_cost_usd = message.total_cost_usd or 0.0
            elif isinstance(message, RateLimitEvent):
                self.last_rate_limit_status = message.rate_limit_info.status
        if text_blocks_seen == 0:
            yield Final(text=NO_REPLY)

    @staticmethod
    def _prompt(
        text: str, attachments: Sequence[Attachment]
    ) -> str | AsyncIterable[dict[str, Any]]:
        """The ``query`` payload: a plain string, or a one-item envelope stream."""
        if not attachments:
            return text
        envelope = _build_user_message(text, attachments)

        async def _stream() -> AsyncIterator[dict[str, Any]]:
            yield envelope

        return _stream()

    async def _reset_to_fresh(self) -> None:
        """Tear down the failed resuming client and rebuild one with no resume."""
        try:
            await self.aclose()
        except Exception:  # the subprocess is already dead; disconnect may error
            logger.debug("aclose during resume reset failed", exc_info=True)
        self._options.resume = None  # ClaudeAgentOptions is a mutable dataclass
        self._client = self._client_factory(self._options)
        self._connected = False
        self.session_id = None

    async def interrupt(self) -> None:
        """Abort the in-flight turn (steering 'stop / do X instead')."""
        await self._client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch the live session's model (owner Opus escalation, M11)."""
        await self._client.set_model(model)

    async def aclose(self) -> None:
        """Disconnect the client, freeing its subprocess."""
        if self._connected:
            await self._client.disconnect()
            self._connected = False
