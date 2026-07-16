"""Native tool interface: in-process tools the agent can call.

Tool handlers are async callables taking keyword arguments from the model's
tool call and returning a string result. Dispatch never raises on bad calls —
errors come back as model-visible result strings so the loop can self-correct.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from chief.provider.base import ToolCall, ToolSpec

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ToolContext:
    """Where a tool call came from; injected into tools that want it."""

    thread_key: str
    channel: str


@dataclass(frozen=True)
class Tool:
    """A registered native tool: its spec plus the coroutine that runs it.

    ``wants_context`` handlers receive a ``context`` keyword with the calling
    session's ToolContext (e.g. so a monitor can wake its own thread).
    """

    spec: ToolSpec
    handler: ToolHandler
    wants_context: bool = False


class ToolDispatcher(Protocol):
    """What the tool loop needs: specs for the model, dispatch for its calls."""

    def specs(self) -> list[ToolSpec]: ...

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str: ...


class ToolRegistry:
    """Holds the tools exposed to the model and dispatches its calls."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        """Add a tool; a duplicate name is a wiring bug unless ``replace``
        (MCP reconnects re-register their tools)."""
        if not replace and tool.spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        """All registered tool specs, for the provider call."""
        return [tool.spec for tool in self._tools.values()]

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        """Run one tool call; any failure returns an error string result."""
        tool = self._tools.get(call.name)
        if tool is None:
            return f"error: unknown tool '{call.name}'"
        kwargs = dict(call.arguments)
        if tool.wants_context:
            kwargs["context"] = context
        try:
            return await tool.handler(**kwargs)
        except TypeError as exc:
            return f"error: bad arguments for '{call.name}': {exc}"
        except Exception as exc:
            logger.exception("tool %s failed with args %s", call.name, call.arguments)
            return f"error: tool '{call.name}' failed: {exc}"
