"""Arrival stamps for transcript messages, in the host's local timezone.

A user or tool message carries ``[YYYY-MM-DD HH:MM:SS TZ, UTC±HH:MM]`` under
``provider.base.STAMP_KEY``, beside its raw ``content``. Stored content stays
exactly what was said — the web history, the cockpit's pending-message match and
compaction's kept tail all read it as written — and only ``for_model`` folds the
stamp in front of it when a model is sent the transcript.

The stamp names when the message *arrived*, not when it was processed: one
queued behind a long turn, injected mid-turn, or replayed after a restart keeps
its own time. On its own that gives the model a wrong clock: an iMessage sent at
09:00 and read at 10:30 would look like 09:00 now, and "in an hour" would become
a time already past. So a user message built more than ``BACKLOG_SECONDS`` after
it arrived also says when it was read, ``[… ; read HH:MM:SS]``, and
``STAMP_NOTE`` tells the model to take that as now. A tool result is built as it
lands, so it never waits and never needs one.

Both times are formatted once, here, and persisted, so the model-facing prefix
is byte-stable and provider prompt caches keep hitting.

Every user and tool message of a transcript is built here, so the stamp is
added in one place (``stamped``).
"""

import time
from datetime import UTC, datetime
from typing import Any

from chief.provider.base import STAMP_KEY

# How long a message may wait between arriving and being built into the
# transcript before its stamp also says when it was read. Within a minute the
# arrival time is as good as "now" for scheduling; past it, a model counting
# from the arrival time lands in the past.
BACKLOG_SECONDS = 60.0

# The one explanation of the stamp, for every system prompt whose model is sent
# stamped messages (the main agent's and each subagent's). It lives beside
# ``stamp`` so a change of format and of its explanation travel together.
STAMP_NOTE = (
    "User messages and tool results begin with a host-added local-time stamp, "
    "[date time zone, UTC offset], saying when they arrived: use it to reason "
    "about time, but never write one yourself or repeat it in a reply. A "
    "message that waited (chief was down or busy) adds '; read <time>' inside "
    "the stamp: the first time is when the owner sent it, the read time when "
    "chief received it, in the same zone (its date or UTC offset is spelled "
    "out only when it differs from the first). The read time, or else the "
    "latest stamp in the conversation, is the closest thing to now: count "
    '"in an hour" or "tomorrow" from that, never from the sent time.'
)


def stamp(at: float | None = None, read: float | None = None) -> str:
    """``[YYYY-MM-DD HH:MM:SS TZ, UTC±HH:MM]`` for the instant ``at`` (epoch
    seconds; now by default), in the host's local zone.

    The zone comes from the system (``TZ`` or the OS-configured zone, on both
    macOS and Linux); the numeric offset lets the model convert exactly even
    when the abbreviation is ambiguous.

    ``read`` — when a message that arrived at ``at`` was read — adds
    ``; read HH:MM:SS`` before the closing bracket, in the same zone. That time
    spells out its date, or its zone, only when they differ from ``at``'s: a
    bare time of day would pass for the same day after a night's downtime, and
    for the same UTC offset across a clock change."""
    arrived = _local(time.time() if at is None else at)
    label = f"{arrived:%Y-%m-%d %H:%M:%S} {_zone(arrived)}"
    if read is not None:
        label += f"; read {_read_time(arrived, _local(read))}"
    return f"[{label}]"


def _local(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC).astimezone()


def _zone(moment: datetime) -> str:
    offset = moment.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}"
    return f"{moment.tzname() or offset_label}, {offset_label}"


def _read_time(arrived: datetime, read: datetime) -> str:
    parts = []
    if read.date() != arrived.date():
        parts.append(f"{read:%Y-%m-%d}")
    parts.append(f"{read:%H:%M:%S}")
    if read.utcoffset() != arrived.utcoffset():
        parts.append(_zone(read))
    return " ".join(parts)


def stamped(
    message: dict[str, Any], at: float | None = None, read: float | None = None
) -> dict[str, Any]:
    """``message`` plus the stamp of the instant ``at`` (now by default), and of
    when it was read, if ``read`` is given."""
    return {**message, STAMP_KEY: stamp(at, read)}


def user_message(text: str, at: float | None = None) -> dict[str, Any]:
    """A user turn: ``text`` raw, stamped with when it arrived (``at``; now by
    default). One built more than ``BACKLOG_SECONDS`` after ``at`` also says when
    it was read: now, the moment it is built."""
    now = time.time()
    waited = at is not None and now - at > BACKLOG_SECONDS
    return stamped({"role": "user", "content": text}, at, now if waited else None)


def tool_message(call_id: str, result: str) -> dict[str, Any]:
    """A tool result: ``result`` raw, stamped with when it landed. It is built
    the moment it lands, so that is already now and it has no read time."""
    return stamped({"role": "tool", "tool_call_id": call_id, "content": result})
