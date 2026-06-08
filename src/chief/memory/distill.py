"""Distill a stale transcript into durable facts (DESIGN: distill on staleness).

A single ``max_turns=1`` SDK call (the one-shot pattern shared with
:func:`chief.core.agent.owner_oneshot` and :func:`chief.core.classify._ask_yes_no`)
reads the current ``facts/`` listing plus the idle conversation and returns a **JSON
array** of durable facts, ignoring ephemeral chatter and flagging time-sensitive ones
with ``expires``. JSON is parsed in code; any failure (SDK error, non-JSON, wrong shape)
logs and writes nothing — distillation must never crash a task.

The agent only *proposes* facts here; the caller persists them through the gate-free
:class:`~chief.memory.store.MemoryStore` Python API (learning never enacts a tool/policy
change — DESIGN's hard safety rule).
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
)
from claude_agent_sdk import (
    query as _sdk_query,
)

from .store import FactDraft

logger = logging.getLogger("chief.memory.distill")

QueryFn = Callable[..., AsyncIterator[Any]]

DISTILL_SYSTEM = (
    "You distill a conversation into durable memory for a personal assistant. "
    "Return ONLY a JSON array (no prose) of the facts worth remembering long-term: "
    "stable preferences, profile details, commitments, relationships. Ignore small "
    "talk, one-off task details, and anything ephemeral unless it has a clear expiry. "
    "Each item is an object with: slug (short kebab-case id), title (one line), body "
    "(the fact, may use [[wikilinks]]), trust (high|medium|low), and optionally "
    "expires (ISO-8601) for time-sensitive facts (e.g. 'on vacation until Monday'). "
    "If nothing is worth keeping, return []."
)


def _render(transcript: list[tuple[str, str]], index: str) -> str:
    convo = "\n".join(f"{speaker}: {text}" for speaker, text in transcript)
    return (
        f"Current memory index:\n{index or '(empty)'}\n\n"
        f"Conversation:\n{convo}\n\n"
        "Return the JSON array of durable facts."
    )


def _strip_fence(raw: str) -> str:
    """Drop a leading/trailing ``` fence the model may wrap the JSON in."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _to_draft(item: Any) -> FactDraft | None:
    if not isinstance(item, dict):
        return None
    slug, title, body = item.get("slug"), item.get("title"), item.get("body")
    if not (isinstance(slug, str) and isinstance(title, str) and isinstance(body, str)):
        return None
    trust = item.get("trust")
    expires = item.get("expires")
    return FactDraft(
        slug=slug,
        title=title,
        body=body,
        trust=trust if isinstance(trust, str) else "medium",
        expires=expires if isinstance(expires, str) else None,
    )


def _parse_drafts(raw: str) -> list[FactDraft]:
    try:
        parsed = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        logger.warning("distill produced non-JSON output; writing nothing")
        return []
    if not isinstance(parsed, list):
        logger.warning("distill output was not a JSON array; writing nothing")
        return []
    return [draft for item in parsed if (draft := _to_draft(item)) is not None]


async def distill(
    transcript: list[tuple[str, str]],
    index: str,
    *,
    model: str,
    query: QueryFn = _sdk_query,
) -> list[FactDraft]:
    """Extract durable :class:`FactDraft`s from an idle ``transcript``.

    Args:
        transcript: ``(speaker, text)`` turns of the idle conversation.
        index: the current ``facts/`` listing so the model overwrites, not dupes.
        model: the extraction model (owner default, Sonnet).
        query: SDK ``query`` callable, injectable for tests.

    Returns:
        The parsed drafts, or ``[]`` on any SDK/parse failure (logged, never raised).
    """
    options = ClaudeAgentOptions(
        max_turns=1, model=model, system_prompt=DISTILL_SYSTEM
    )
    parts: list[str] = []
    try:
        async for message in query(prompt=_render(transcript, index), options=options):
            if isinstance(message, AssistantMessage):
                parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    except Exception:
        logger.warning("distill query failed; writing nothing", exc_info=True)
        return []
    return _parse_drafts("".join(parts))
