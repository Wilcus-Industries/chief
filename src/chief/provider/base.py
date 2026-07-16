"""The LLM provider seam.

Every model backend implements ``Provider``: a single streaming chat call that
yields ``TextDelta`` events as tokens arrive and terminates with exactly one
``Completion`` carrying the assembled assistant turn. Messages use the
OpenAI-style wire format (``role``/``content``/``tool_calls``/``tool_call_id``
dicts) — that dict shape is part of this seam's contract.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A model call failed (HTTP error, malformed stream, refused request)."""


@dataclass(frozen=True)
class ToolSpec:
    """Declaration of a callable tool: name, description, JSON-schema params."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TextDelta:
    """A streamed chunk of assistant text."""

    text: str


@dataclass(frozen=True)
class Usage:
    """Token/cost accounting for one model call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost=self.cost + other.cost,
        )


@dataclass(frozen=True)
class Completion:
    """Terminal stream event: the fully assembled assistant turn."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()


ProviderEvent = TextDelta | Completion


class Provider(Protocol):
    """Streaming chat interface every model backend implements."""

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one model turn; yield TextDeltas, end with one Completion."""
        ...
