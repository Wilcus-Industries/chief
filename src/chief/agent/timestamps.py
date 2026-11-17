"""Arrival stamps for transcript messages, in the host's local timezone.

A user or tool message carries ``[YYYY-MM-DD HH:MM:SS TZ, UTC±HH:MM]`` under
``provider.base.STAMP_KEY``, beside its raw ``content``. Stored content stays
exactly what was said — the web history, the cockpit's pending-message match and
compaction's kept tail all read it as written — and only ``for_model`` folds the
stamp in front of it when a model is sent the transcript.

The stamp names when the message *arrived*, not when it was processed: one
queued behind a long turn, injected mid-turn, or replayed after a restart keeps
its own time. It is formatted once, here, and persisted, so the model-facing
prefix is byte-stable and provider prompt caches keep hitting.

Every user and tool message of a transcript is built here, so the stamp is
added in one place (``stamped``).
"""

import time
from datetime import UTC, datetime
from typing import Any

from chief.provider.base import STAMP_KEY


def stamp(at: float | None = None) -> str:
    """``[YYYY-MM-DD HH:MM:SS TZ, UTC±HH:MM]`` for the instant ``at`` (epoch
    seconds; now by default), in the host's local zone.

    The zone comes from the system (``TZ`` or the OS-configured zone, on both
    macOS and Linux); the numeric offset lets the model convert exactly even
    when the abbreviation is ambiguous."""
    seconds = time.time() if at is None else at
    moment = datetime.fromtimestamp(seconds, UTC).astimezone()
    offset = moment.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}"
    zone = moment.tzname() or offset_label
    return f"[{moment:%Y-%m-%d %H:%M:%S} {zone}, {offset_label}]"


def stamped(message: dict[str, Any], at: float | None = None) -> dict[str, Any]:
    """``message`` plus the stamp of the instant ``at`` (now by default)."""
    return {**message, STAMP_KEY: stamp(at)}


def user_message(text: str, at: float | None = None) -> dict[str, Any]:
    """A user turn: ``text`` raw, stamped with when it arrived (``at``)."""
    return stamped({"role": "user", "content": text}, at)


def tool_message(call_id: str, result: str) -> dict[str, Any]:
    """A tool result: ``result`` raw, stamped with when it landed."""
    return stamped({"role": "tool", "tool_call_id": call_id, "content": result})
