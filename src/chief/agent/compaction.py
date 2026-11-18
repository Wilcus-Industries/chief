"""Context compaction: fold old history into a system note, keep the tail.

When a thread's transcript nears the model's context window, everything but the
most recent messages is summarized by the model into one system note that then
leads the transcript; the persisted history is truncated to match. Token counts
are a chars/4 estimate — coarse, but it only needs to keep us far from the
window edge.

The threshold is ``ratio * window``, where the window tracks the thread's
*current* model (resolved per turn by :class:`~chief.agent.windows.WindowResolver`)
and ``ratio``/``keep_recent`` come from the ``compaction:`` config block — all
owner-configurable. ``force=True`` (the ``/compact`` command, nightly autocompact)
bypasses the threshold and compacts whatever old history exists.

A thread whose audience is more than the owner (a group chat, a friend's 1:1)
also compacts once its messages alone pass ``message_budget`` tokens — the
system prompt and the summary note are not counted. Those threads are where a
long transcript costs most and helps least; owner threads keep the window.
"""

import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from chief.provider.base import Completion, Provider, for_model
from chief.provider.images import IMAGE_TOKENS, image_count

Budgeted = Callable[[str, str], bool]  # (thread_key, channel) -> under the budget?

logger = logging.getLogger(__name__)

DEFAULT_RATIO = 0.95
KEEP_RECENT = 20
NOTE_PREFIX = "[compacted history summary]\n"

SUMMARY_INSTRUCTION = (
    "Summarize this conversation history for your own future reference. "
    "Keep every commitment, open task, owner preference, and hard fact "
    "(names, paths, numbers, decisions). Drop pleasantries and dead ends. "
    "Write a dense note, not prose for a human."
)


class WindowSource(Protocol):
    """Resolves a model name to its context window in tokens."""

    async def resolve(self, model: str) -> int: ...


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count: serialized length over four, plus a flat
    :data:`IMAGE_TOKENS` per stored photo."""
    text = sum(len(json.dumps(m)) for m in for_model(messages)) // 4
    return text + IMAGE_TOKENS * image_count(messages)


def message_tokens(messages: list[dict[str, Any]]) -> int:
    """What the message budget counts: the transcript minus a leading summary
    note (the system prompt is never in it)."""
    if messages and is_note(messages[0]):
        messages = messages[1:]
    return estimate_tokens(messages)


def is_note(message: dict[str, Any]) -> bool:
    return message.get("role") == "system" and str(
        message.get("content", "")).startswith(NOTE_PREFIX)


class Compactor:
    """Summarizes a long transcript down to a note plus its recent tail."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        resolver: WindowSource,
        *,
        ratio: float = DEFAULT_RATIO,
        keep_recent: int = KEEP_RECENT,
        message_budget: int = 0,
        budgeted: Budgeted | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._resolver = resolver
        self._ratio = ratio
        self._keep_recent = keep_recent
        self._message_budget = message_budget
        self._budgeted = budgeted

    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        force: bool = False,
        thread: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]] | None:
        """The compacted transcript, or None when nothing needs compacting.

        ``model`` is the thread's current model, used to look up the window;
        it defaults to the summarizer model. ``force`` bypasses the threshold.
        ``thread`` is ``(thread_key, channel)``, for the message budget.
        """
        if not force and not await self._due(messages, model, thread):
            return None
        split = self._split_index(messages)
        old, recent = messages[:split], messages[split:]
        if not old:
            return None
        summary = await self._summarize(old)
        if not summary:
            # A model refusal / content-filter / empty stream yields no summary.
            # Truncating to an empty note would permanently discard the history
            # for nothing — especially bad on the unattended nightly force path.
            logger.warning("compaction aborted: summarizer returned empty")
            return None
        logger.info(
            "compacted %d messages into a note, kept %d", len(old), len(recent)
        )
        return [{"role": "system", "content": NOTE_PREFIX + summary}, *recent]

    async def _due(
        self, messages: list[dict[str, Any]], model: str | None,
        thread: tuple[str, str] | None,
    ) -> bool:
        if (
            self._message_budget > 0 and thread is not None
            and self._budgeted is not None and self._budgeted(*thread)
            and message_tokens(messages) >= self._message_budget
        ):
            return True
        window = await self._resolver.resolve(model or self._model)
        return estimate_tokens(messages) >= int(self._ratio * window)

    def _split_index(self, messages: list[dict[str, Any]]) -> int:
        """First user message at or past the tail mark — a turn boundary, so
        no assistant tool call is ever severed from its tool results."""
        candidate = max(0, len(messages) - self._keep_recent)
        for i in range(candidate, len(messages)):
            if messages[i].get("role") == "user":
                return i
        return len(messages)

    async def _summarize(self, old: list[dict[str, Any]]) -> str:
        transcript = json.dumps(for_model(old), ensure_ascii=False)
        request = [
            {"role": "system", "content": SUMMARY_INSTRUCTION},
            {"role": "user", "content": transcript},
        ]
        text = ""
        async for event in self._provider.stream(
            model=self._model, messages=request, tools=[]
        ):
            if isinstance(event, Completion):
                text = event.text
        return text.strip()
