"""Deterministic fake provider — the one scripted fake CI is allowed (PRD #183).

It implements the same seam as real providers, so integration tests drive the
real daemon round-trip with only the model call swapped out.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from chief.provider.base import (
    Completion,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)


class FakeProvider:
    """Plays back scripted event streams, one script entry per model call.

    ``tool_specs`` holds the tool specs offered on each call, so a boot test
    can prove which tools actually reached the model.
    """

    def __init__(self, script: list[list[ProviderEvent]]) -> None:
        self._script = list(script)
        self.calls: list[list[dict[str, Any]]] = []
        self.models: list[str] = []
        self.tool_specs: list[list[ToolSpec]] = []
        # Optional handshake for concurrency tests: when set, each call waits
        # here after recording itself, so tests control interleaving.
        self.gate: asyncio.Event | None = None

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        self.models.append(model)
        self.calls.append([dict(m) for m in messages])
        self.tool_specs.append(list(tools))
        if self.gate is not None:
            await self.gate.wait()
        for event in self._script.pop(0):
            yield event


def text_turn(text: str) -> list[ProviderEvent]:
    """Script entry for a plain streamed text reply."""
    deltas: list[ProviderEvent] = [TextDelta(c) for c in _split(text)]
    return [*deltas, Completion(text=text)]


def tool_turn(
    name: str, arguments: dict[str, Any], call_id: str = "call-1"
) -> list[ProviderEvent]:
    """Script entry: the model requests one tool call and no text."""
    return [
        Completion(
            text="", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),)
        )
    ]


def _split(text: str) -> list[str]:
    mid = max(1, len(text) // 2)
    return [text[:mid], text[mid:]] if len(text) > 1 else [text]
