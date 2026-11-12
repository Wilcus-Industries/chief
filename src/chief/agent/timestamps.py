"""Timestamp prefix for transcript messages, in the host's local timezone.

Stamping at creation (not at send time) keeps each stamp equal to when the
message actually happened and keeps the transcript prefix byte-stable across
model calls, so provider prompt caches still hit.
"""

from datetime import datetime


def stamp(text: str, now: datetime | None = None) -> str:
    """Return ``text`` prefixed with ``[YYYY-MM-DD HH:MM:SS TZ, UTC±HH:MM]``.

    The zone comes from the system (``TZ`` or the OS-configured zone, on both
    macOS and Linux); the numeric offset lets the model convert exactly even
    when the abbreviation is ambiguous.
    """
    moment = (now or datetime.now()).astimezone()
    offset = moment.strftime("%z")
    offset_label = f"UTC{offset[:3]}:{offset[3:]}"
    zone = moment.tzname() or offset_label
    return f"[{moment:%Y-%m-%d %H:%M:%S} {zone}, {offset_label}] {text}"
