"""Native tool interface: in-process tools the agent can call.

Tool handlers are async callables taking keyword arguments from the model's
tool call and returning a string result. Dispatch never raises on bad calls —
errors come back as model-visible result strings so the loop can self-correct.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from chief.provider.base import ToolCall, ToolSpec

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    """A registered native tool: its spec plus the coroutine that runs it."""

    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """Holds the tools exposed to the model and dispatches its calls."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool; a duplicate name is a wiring bug, so it raises."""
        if tool.spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        """All registered tool specs, for the provider call."""
        return [tool.spec for tool in self._tools.values()]

    async def dispatch(self, call: ToolCall) -> str:
        """Run one tool call; any failure returns an error string result."""
        tool = self._tools.get(call.name)
        if tool is None:
            return f"error: unknown tool '{call.name}'"
        try:
            return await tool.handler(**call.arguments)
        except TypeError as exc:
            return f"error: bad arguments for '{call.name}': {exc}"
        except Exception as exc:
            logger.exception("tool %s failed with args %s", call.name, call.arguments)
            return f"error: tool '{call.name}' failed: {exc}"
