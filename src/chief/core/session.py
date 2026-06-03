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

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookEvent

from .agent import NO_REPLY


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
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self._options = ClaudeAgentOptions(
            model=model,
            resume=resume,
            can_use_tool=can_use_tool,
            hooks=hooks,
        )
        self._client = client_factory(self._options)
        self._connected = False
        #: Resumable SDK session id, populated from the first turn's stream.
        self.session_id: str | None = resume

    async def _ensure_connected(self) -> None:
        if not self._connected:
            await self._client.connect()
            self._connected = True

    async def run_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Send ``text`` and stream the response as milestone/final events."""
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
