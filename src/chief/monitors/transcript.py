"""Render thread lines as the chat log a wake (and a judge) reads.

One line per message, time then speaker then text::

    ▶ 5:06 PM  Henry: "Ur an idiot chief"

Who said what must be unmistakable, so the speaker comes first and is always
set by chief: ``you`` for chief's own sends, the roster's first name (full
name when two speakers share one), ``(owner)`` decided from the handle, and
the bare handle only for someone the roster does not know. Parentheses never
survive into a name or handle, so ``(owner)`` cannot be forged, and a speaker
named like ``you`` is tagged ``(contact)``. The text is a JSON string with
every line separator escaped, so a message can never start a forged line.
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime

from chief.contacts import Roster
from chief.monitors.history import Line
from chief.policy import normalize_handle

NEW_MARK = "▶ "
WITHHELD = "[text withheld]"

_UNSAFE = re.compile(r"[^\w@+.:;\-]")  # a handle shown bare: no free text
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")
# Separators json.dumps leaves raw with ensure_ascii=False.
_SEPARATORS = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}
RESERVED_LABELS = ("you", "owner")


class Namer:
    """Speaker labels for lines, from the owner's roster and owner handles."""

    def __init__(
        self, roster: Roster | None = None, owner_handles: Sequence[str] = ()
    ) -> None:
        self._roster = roster
        self._owner = frozenset(normalize_handle(h) for h in owner_handles if h)

    def trusted(self, line: Line) -> bool:
        """Chief's own words and the owner's need no injection screen."""
        return line.mine or self.is_owner(line.sender)

    def is_owner(self, handle: str) -> bool:
        return bool(handle) and normalize_handle(handle) in self._owner

    def labels(self, lines: Sequence[Line]) -> dict[str, str]:
        """``{handle: label}`` for every non-chief speaker in ``lines``."""
        full = {
            line.sender: self._full_name(line.sender)
            for line in lines if not line.mine
        }
        firsts = [name.split()[0] for name in full.values() if name]
        labels = {}
        for handle, name in full.items():
            first = name.split()[0] if name else ""
            label = (name if firsts.count(first) > 1 else first) if name else ""
            label = label or _UNSAFE.sub("", handle)[:64] or "someone"
            if self.is_owner(handle):
                label = f"{label} (owner)" if name else "(owner)"
            elif label.casefold() in RESERVED_LABELS:
                label = f"{label} (contact)"
            labels[handle] = label
        return labels

    def legend(self, lines: Sequence[Line]) -> str:
        """``Henry = +1555…`` for each named speaker, so the handle (the
        identity monitors scope on) is still one glance away."""
        pairs = [
            f"{label} = {_UNSAFE.sub('', handle)[:64]}"
            for handle, label in self.labels(lines).items()
            if label != _UNSAFE.sub("", handle)[:64]
        ]
        return "; ".join(pairs)

    def _full_name(self, handle: str) -> str:
        name = self._roster.name_for(handle) if self._roster else None
        # A roster entry may carry its own note ("Will (owner)"); the owner
        # tag is decided from the handle here, never taken from the file.
        name = re.sub(r"\(.*?\)", "", _CONTROL.sub(" ", name or ""))
        return " ".join(name.split())[:64]


def render(
    lines: Sequence[Line], namer: Namer, *, new_after: int | None,
    withhold: bool = False, clock: Callable[[], float] = time.time,
) -> str:
    """The chat log for ``lines``. Lines past ``new_after`` from anyone but
    chief carry :data:`NEW_MARK` (``None``: none are new). ``withhold``
    replaces every untrusted line's text with :data:`WITHHELD`."""
    labels = namer.labels(lines)
    today = datetime.fromtimestamp(clock()).date()
    out = []
    for line in lines:
        new = new_after is not None and line.id > new_after and not line.mine
        who = "you" if line.mine else labels[line.sender]
        if line.shared:  # a send from the account chief shares with the owner
            who = f"{who} or you"
        text = _quote(line.text)
        if withhold and not namer.trusted(line):
            text = WITHHELD
        mark = NEW_MARK if new else ""
        out.append(f"{mark}{_when(line.at, today)}  {who}: {text}")
    return "\n".join(out)


def _quote(text: str) -> str:
    quoted = json.dumps(text, ensure_ascii=False)
    for raw, escaped in _SEPARATORS.items():
        quoted = quoted.replace(raw, escaped)
    return quoted


def _when(at: float, today: date) -> str:
    moment = datetime.fromtimestamp(at)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    if moment.date() == today:
        return clock
    return f"{moment.strftime('%b')} {moment.day} {clock}"
