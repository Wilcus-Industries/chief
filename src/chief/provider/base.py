"""The LLM provider seam.

Every model backend implements ``Provider``: a single streaming chat call that
yields ``TextDelta`` events as tokens arrive and terminates with exactly one
``Completion`` carrying the assembled assistant turn. Messages use the
OpenAI-style wire format (``role``/``content``/``tool_calls``/``tool_call_id``
dicts) — that dict shape is part of this seam's contract.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from chief.provider.images import IMAGES_KEY, loadable, with_images


class ProviderError(RuntimeError):
    """A model call failed (HTTP error, malformed stream, refused request)."""


@dataclass(frozen=True)
class ToolSpec:
    """Declaration of a callable tool: name, description, JSON-schema params.

    ``read_only`` marks a pure read — no filesystem, network, state, or send.
    The gate auto-approves these so they never raise an approval card (#187);
    it must stay false for anything that mutates or reaches outward.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool = False


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
class ReasoningDelta:
    """A streamed chunk of the model's reasoning (thinking) — display only."""

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
    reasoning: str = ""


ProviderEvent = TextDelta | ReasoningDelta | Completion

# Transcript keys kept for the owner's eyes only (the web UI's thinking rows):
# stored with a message, but never part of what a model is sent or measured on.
DISPLAY_ONLY_KEYS = ("reasoning",)

# A user or tool message's arrival stamp (``chief.agent.timestamps``): kept
# beside its raw ``content`` and folded in front of it only by ``for_model``, so
# every other reader of stored content sees what was actually said.
STAMP_KEY = "stamp"


def for_model(
    messages: list[dict[str, Any]], *, images: bool = False
) -> list[dict[str, Any]]:
    """``messages`` as a model may see them: display-only keys dropped, and each
    stored stamp moved in front of its content. The stored dicts are untouched.

    The stamp was formatted once, when its message arrived, and is only
    prefixed here — so a message reaches the model byte-identical on every
    call, which is what keeps provider prompt caches hitting. ``images`` loads
    stored photo paths as content parts (only the real model call does);
    without it they read as ``[photo]`` stubs (see ``provider.images``)."""
    load = loadable(messages) if images else {}
    return [_for_model(m, load.get(i, 0)) for i, m in enumerate(messages)]


def _for_model(message: dict[str, Any], images: int) -> dict[str, Any]:
    hidden = (*DISPLAY_ONLY_KEYS, STAMP_KEY, IMAGES_KEY)
    if not any(k in message for k in hidden):
        return message
    shaped = {k: v for k, v in message.items() if k not in hidden}
    if STAMP_KEY in message:
        shaped["content"] = f"{message[STAMP_KEY]} {message['content']}"
    if paths := message.get(IMAGES_KEY):
        shaped["content"] = with_images(str(shaped.get("content") or ""),
                                        list(paths), images)
    return shaped


def parse_arguments(raw: str) -> dict[str, Any]:
    """Parse streamed tool-argument JSON; surface malformed output to the tool layer."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_unparsed_arguments": raw}
    if not isinstance(parsed, dict):
        return {"_unparsed_arguments": raw}
    return parsed


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
