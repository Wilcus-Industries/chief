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

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ProcessError,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookEvent, McpServerConfig

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


class _Client(Protocol):
    """The slice of :class:`ClaudeSDKClient` the session uses (structural)."""

    async def connect(self) -> None: ...
    async def query(self, prompt: str, session_id: str = ...) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str | None = ...) -> None: ...
    async def disconnect(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], _Client]


def _default_client(options: ClaudeAgentOptions) -> _Client:
    return ClaudeSDKClient(options)


class TaskSession:
    """One task's live SDK conversation."""

    def __init__(
        self,
        *,
        model: str,
        resume: str | None = None,
        can_use_tool: CanUseTool | None = None,
        hooks: dict[HookEvent, list[HookMatcher]] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict[str, McpServerConfig] | None = None,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self._options = ClaudeAgentOptions(
            model=model,
            resume=resume,
            can_use_tool=can_use_tool,
            hooks=hooks,
            system_prompt=system_prompt,
            cwd=cwd,
            allowed_tools=allowed_tools if allowed_tools is not None else [],
            disallowed_tools=disallowed_tools if disallowed_tools is not None else [],
            mcp_servers=mcp_servers if mcp_servers is not None else {},
        )
        self._client_factory = client_factory
        self._client = client_factory(self._options)
        self._connected = False
        #: Resumable SDK session id, populated from the first turn's stream.
        self.session_id: str | None = resume

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def run_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Send ``text`` and stream the response as milestone/final events.

        A dead/expired resume id makes the CLI exit non-zero on the turn's first
        message. If nothing has streamed yet and we were resuming, drop the resume and
        retry once on a fresh session rather than crashing the whole turn — the new id
        is then persisted by the engine, healing the stale pointer. (A genuine
        first-message ``ProcessError`` on a resuming turn also restarts fresh; an
        acceptable degradation vs. a hard crash.)
        """
        streamed = False
        try:
            async for event in self._stream_once(text):
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
            async for event in self._stream_once(text):
                yield event

    async def _stream_once(self, text: str) -> AsyncIterator[TurnEvent]:
        """One streamed turn against the current client (no resume fallback)."""
        await self._ensure_connected()
        await self._client.query(text)
        parts: list[str] = []
        async for message in self._client.receive_response():
            session_id = getattr(message, "session_id", None)
            if session_id:
                self.session_id = session_id
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield Milestone(text=f"using {block.name}")
        yield Final(text="".join(parts).strip() or NO_REPLY)

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
