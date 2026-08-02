"""What the gate's lists decide, and how "always allow" is persisted.

Split from :mod:`chief.gate` so that file stays the enforcement path (cards,
announcements, audit) and this one holds the decision rules and their storage.

Two stores feed one effective approved set: ``gate.approved`` in config.yaml is
the owner's declared intent, and ``gate_approved.json`` accretes "always allow"
answers. They are unioned at boot (:func:`chief.wiring.build_gate`), which is
what lets a tap take effect without ever rewriting the owner's config.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# An argument-scoped grant is one approved-set entry, so "always allow" on an
# ask_when card persists through the same file and the same code path as any
# other. Tool names are identifiers or "mcp_<server>_<tool>", so ":" cannot
# collide with a bare name.
GRANT_SEPARATOR = ":"


def grant_key(tool_name: str, argument: str) -> str:
    """The approved-set entry standing for "this tool, carrying this argument"."""
    return f"{tool_name}{GRANT_SEPARATOR}{argument}"


class Decision(Enum):
    NEVER = "never"
    APPROVED = "approved"
    ASK = "ask"


@dataclass
class GatePolicy:
    """The code-enforced lists; anything on neither list asks.

    ``approved`` is a live set — an "always allow" answer adds to it so the
    tool stops asking for the rest of the process (and is persisted so it
    survives a restart). A ``"*"`` entry in ``approved`` matches every tool
    name (the config-driven "all tools" switch); ``never`` still takes
    precedence over it.

    ``ask_when`` maps a tool name to argument names that pull it back into the
    card path *however* it was approved. It exists because one tool can be two
    actions: hound's ``smart_fetch`` reads a page, but the same call carrying
    ``actions`` clicks and submits on it. Answering "always" to such a card
    grants ``tool:argument`` (see :func:`grant_key`) rather than the bare name,
    so the owner never has to approve the whole tool to allow one argument.
    """

    never: frozenset[str] = frozenset()
    approved: set[str] = field(default_factory=set)
    ask_when: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def decide(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> Decision:
        if tool_name in self.never:
            return Decision.NEVER
        if self._watched_present(tool_name, arguments):
            # A call carrying a watched argument is decided by its grants
            # alone — the bare name is neither required nor sufficient. This
            # must come *before* the bare-name branch: a composite grant only
            # lifts the veto, so falling through would leave "always" a no-op
            # that re-cards forever on a tool nothing else approves.
            if self.pending_arguments(tool_name, arguments):
                return Decision.ASK
            return Decision.APPROVED
        if "*" in self.approved or tool_name in self.approved:
            return Decision.APPROVED
        return Decision.ASK

    def _watched_present(
        self, tool_name: str, arguments: Mapping[str, Any] | None
    ) -> tuple[str, ...]:
        """The watched argument names this call actually carries."""
        watched = self.ask_when.get(tool_name) or ()
        return tuple(name for name in watched if name in (arguments or {}))

    def pending_arguments(
        self, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> tuple[str, ...]:
        """Watched arguments this call carries that have no standing grant.

        Non-empty means the call must be carded no matter what the lists say.
        """
        return tuple(
            name
            for name in self._watched_present(tool_name, arguments)
            if grant_key(tool_name, name) not in self.approved
        )

    def allow_always(self, tool_name: str) -> None:
        self.approved.add(tool_name)


def load_approved(path: Path) -> set[str]:
    """Read the persisted "always allow" entries (empty if absent)."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_approved(names: set[str], path: Path) -> None:
    """Persist the "always allow" entries, sorted for a stable file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(names)))
