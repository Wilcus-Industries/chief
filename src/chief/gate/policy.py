"""Self-curating allowlist + the safe-matcher (DESIGN's flagged hard part).

The "always allow / always deny" buttons mutate policy, so the writer must never admit a
rule broader than what the owner actually approved. Two defences:

- **Parse, never execute.** A shell command is split with :mod:`shlex` into
  ``(binary, args)`` — the string is never handed to a shell. An entry carrying shell
  metacharacters (``;`` ``|`` ``>`` ``$`` …) is rejected: one ``(binary, arg-shape)``
  rule cannot honestly stand for a compound/piped command.
- **No wildcards on destructive binaries.** ``rm -rf *`` matches the *string* but its
  *effect* depends on the cwd, so a glob argument to ``rm``/``dd``/``mkfs`` is rejected.

Matching is exact on the canonical ``(tool, arg_pattern)``: a stored rule only ever
auto-decides an identical call. ``arg_pattern is None`` is a whole-tool rule (e.g. NEVER
the ``WebFetch`` tool outright).
"""

import json
import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence.policy import APPROVED, NEVER, add_entry, list_entries

#: Tools whose effect is a shell command string under ``tool_input["command"]``.
COMMAND_TOOLS = frozenset({"Bash"})

#: Shell features a single safe-matched rule cannot faithfully represent.
_METACHARACTERS = frozenset(";|&<>$`()\n{}")
#: Glob characters whose expansion makes a destructive command non-deterministic.
_WILDCARDS = frozenset("*?[")
#: Binaries where a wildcard argument turns a typo into data loss.
DESTRUCTIVE_BINARIES = frozenset(
    {"rm", "rmdir", "dd", "shred", "mkfs", "mke2fs", "fdisk", "parted", "wipefs"}
)


def parse_command(command: str) -> tuple[str, list[str]]:
    """Split ``command`` into ``(binary, args)`` with :mod:`shlex` (never executed).

    Raises:
        ValueError: if the command cannot be lexed (e.g. unbalanced quotes).
    """
    tokens = shlex.split(command)
    if not tokens:
        return "", []
    return tokens[0], tokens[1:]


def canonical_command(command: str) -> str:
    """Whitespace-normalized re-join of ``command`` (falls back to the raw string)."""
    try:
        binary, args = parse_command(command)
    except ValueError:
        return command
    return shlex.join([binary, *args]) if binary else ""


def is_safe_command(command: str) -> bool:
    """Whether ``command`` may become a permanent ``(binary, arg-shape)`` rule."""
    if any(ch in _METACHARACTERS for ch in command):
        return False
    try:
        binary, args = parse_command(command)
    except ValueError:
        return False
    if not binary:
        return False
    if os.path.basename(binary) in DESTRUCTIVE_BINARIES:
        if any(any(ch in _WILDCARDS for ch in arg) for arg in args):
            return False
    return True


def is_safe_entry(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Whether a policy rule derived from this call is safe to persist.

    Command tools are validated by :func:`is_safe_command`. Other tools match by exact
    canonical input, so their metacharacters are inert data — always safe to store.
    """
    if tool_name in COMMAND_TOOLS:
        return is_safe_command(str(tool_input.get("command", "")))
    return True


def derive_pattern(tool_name: str, tool_input: dict[str, Any]) -> str:
    """The canonical ``arg_pattern`` stored/compared for this call."""
    if tool_name in COMMAND_TOOLS:
        return canonical_command(str(tool_input.get("command", "")))
    return json.dumps(tool_input, sort_keys=True, default=str)


def matches(
    tool: str,
    arg_pattern: str | None,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """Whether the stored ``(tool, arg_pattern)`` rule covers this call."""
    if tool != tool_name:
        return False
    if arg_pattern is None:
        return True  # whole-tool rule
    return derive_pattern(tool_name, tool_input) == arg_pattern


@dataclass(frozen=True)
class _Entry:
    list_name: str
    tool: str
    arg_pattern: str | None


class PolicyStore:
    """In-memory NEVER/APPROVED lists, backed by the ``policy`` table.

    Loaded once on boot and kept in memory so :meth:`classify_against` is a synchronous
    hot-path call inside the gate; mutations (``add_allow``/``add_deny``) write through
    to the table and update the cache.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        audit: Any | None = None,
    ) -> None:
        self._sf = session_factory
        self._audit = audit
        self._entries: list[_Entry] = []

    async def load(self) -> None:
        """Replace the in-memory cache from the table."""
        entries: list[_Entry] = []
        async with self._sf() as session:
            for name in (NEVER, APPROVED):
                for row in await list_entries(session, name):
                    entries.append(_Entry(name, row.tool, row.arg_pattern))
        self._entries = entries

    async def seed(
        self,
        *,
        never: Iterable[tuple[str, str | None]] = (),
        approved: Iterable[tuple[str, str | None]] = (),
    ) -> None:
        """Idempotently insert seed rules from config, then reload the cache."""
        async with self._sf() as session:
            for tool, pattern in never:
                await add_entry(
                    session, list_name=NEVER, tool=tool, arg_pattern=pattern
                )
            for tool, pattern in approved:
                await add_entry(
                    session, list_name=APPROVED, tool=tool, arg_pattern=pattern
                )
        await self.load()

    def classify_against(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str | None:
        """Return ``NEVER`` (wins), ``APPROVED``, or ``None`` if nothing matches."""
        matched: str | None = None
        for entry in self._entries:
            if matches(entry.tool, entry.arg_pattern, tool_name, tool_input):
                if entry.list_name == NEVER:
                    return NEVER
                matched = APPROVED
        return matched

    async def add_allow(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Persist an APPROVED rule for this call; ``False`` if it is not safe."""
        return await self._add(APPROVED, tool_name, tool_input)

    async def add_deny(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """Persist a NEVER rule for this call; ``False`` if it is not safe."""
        return await self._add(NEVER, tool_name, tool_input)

    async def _add(
        self, list_name: str, tool_name: str, tool_input: dict[str, Any]
    ) -> bool:
        if not is_safe_entry(tool_name, tool_input):
            if self._audit is not None:
                self._audit.log(
                    {
                        "event": "policy_rejected",
                        "list": list_name,
                        "tool": tool_name,
                    }
                )
            return False
        pattern = derive_pattern(tool_name, tool_input)
        async with self._sf() as session:
            await add_entry(
                session, list_name=list_name, tool=tool_name, arg_pattern=pattern
            )
        entry = _Entry(list_name, tool_name, pattern)
        if entry not in self._entries:
            self._entries.append(entry)
        return True
