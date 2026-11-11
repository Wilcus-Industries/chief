"""Deterministic fake provider — the one scripted fake CI is allowed (PRD #183).

It implements the same seam as real providers, so integration tests drive the
real daemon round-trip with only the model call swapped out.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from chief.adapters.base import Adapter
from chief.provider.base import (
    Completion,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from chief.typesafe import TypeSafeClient


@dataclass
class _Route:
    marker: str
    script: list[list[ProviderEvent]]
    hold: asyncio.Event | None


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
        self._routes: list[_Route] = []

    def route(
        self, marker: str, script: list[list[ProviderEvent]],
        hold: asyncio.Event | None = None,
    ) -> None:
        """Play calls whose messages contain ``marker`` from ``script`` instead
        of the shared one (first matching route wins) — for multi-session tests
        whose turns interleave in an order the shared FIFO can't pin down.
        ``hold`` parks this route's *first* call (after recording it) until set,
        so a test can keep one session mid-turn."""
        self._routes.append(_Route(marker, list(script), hold))

    def _route_for(self, messages: list[dict[str, Any]]) -> "_Route | None":
        seen = json.dumps(messages, default=str)
        return next((r for r in self._routes if r.marker in seen), None)

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
        route = self._route_for(messages)
        if route is not None and route.hold is not None:
            hold, route.hold = route.hold, None
            await hold.wait()
        script = route.script if route is not None else self._script
        for event in script.pop(0):
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


class CapturingAdapter(Adapter):
    """A registered origin adapter that captures its outbound sends and
    deltas — the fake device transport at the edge (#277). Everything upstream
    of ``send`` (web route, dispatcher, session turn) is the real wiring."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[str, str]] = []
        self.deltas: list[tuple[str, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, thread_key: str, text: str) -> None:
        self.sent.append((thread_key, text))

    async def send_delta(self, thread_key: str, text: str) -> None:
        self.deltas.append((thread_key, text))


TypeSafeReply = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | int]


def typesafe_stub(
    reply: TypeSafeReply,
) -> tuple[TypeSafeClient, list[dict[str, Any]]]:
    """A real :class:`TypeSafeClient` over ``httpx.MockTransport`` — not a
    second fake. ``reply(state, questions)`` returns the raw ``answers`` JSON,
    or an int HTTP status to fail with. Returns the client and the request
    bodies it sent."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        answers = reply(body["state"], body["questions"])
        if isinstance(answers, int):
            return httpx.Response(answers, text="stub failure")
        return httpx.Response(200, json={
            "answers": answers, "usage": {"input_tokens": 10, "output_tokens": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TypeSafeClient("ts-test", client=client), bodies
