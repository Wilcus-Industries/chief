"""The approval blacklist — what still needs a card under the default-allow gate.

The host-native posture inverts the old default-ask gate for the owner: effectful tool
calls run freely, and only a call matching this blacklist raises an approval card.
Two kinds of entry:

- **Shell patterns** — regexes matched (``re.search``) against the command string of a
  command tool (:data:`~chief.gate.policy.COMMAND_TOOLS`). The defaults cover the
  classically destructive shapes: sudo/doas, ``rm -rf`` on a root, writing to raw
  devices, shutdown/reboot, killing PID 1, pipe-to-shell installers, force-pushing main,
  ``chmod -R 777``, and global package installs.
- **Tool names** — whole tools that always ask, regardless of input.

A match means ASK (a card), never DENY — over-broad patterns cost one tap, so the
defaults lean broad. Both lists are configurable in ``config.yaml``
(``blacklist_shell_patterns`` / ``blacklist_tools``).
"""

import re
from dataclasses import dataclass
from typing import Any

from .policy import COMMAND_TOOLS

#: Default shell-command patterns that require owner approval. Each is a regex applied
#: with ``re.search`` to the raw command string. Deliberately broad: a false positive
#: costs one approval tap, a false negative costs a machine.
DEFAULT_SHELL_PATTERNS: tuple[str, ...] = (
    # Privilege escalation.
    r"\b(sudo|doas)\b",
    # rm with a recursive flag aimed at a filesystem root, $HOME, or ~.
    r"\brm\b(?=.*\s-\w*[rR])\s.*\s(/|~/?|\"?\$HOME\"?/?)\s*(\s|$|;|&&)",
    # Filesystem creation / raw writes to device nodes.
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^|;&]*\bof=/dev/",
    # Host power state.
    r"\b(shutdown|reboot|poweroff|halt)\b",
    # Killing init.
    r"\bkill\b[^|;&]*(-9|-KILL|-s\s+KILL)\s+1\b",
    # Pipe-to-shell installers (curl ... | sh and friends).
    r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|fi|k)?sh\b",
    # Force-pushing a protected branch.
    r"\bgit\s+push\b(?=.*(--force\b|\s-f\b))(?=.*\b(main|master)\b)",
    # World-writable recursive chmod.
    r"\bchmod\b(?=.*\s-\w*R)(?=.*\b777\b)",
    # Global package installs (system package managers; npm-family -g).
    r"\b(apt|apt-get|dnf|yum|pacman|zypper|apk)\b(\s+\S+)*\s+(install|add|-S\w*)\b",
    r"\b(npm|yarn|pnpm)\b[^|;&]*(\s-g\b|--global\b)",
    r"\bbrew\s+(install|uninstall|rm|remove)\b",
)


@dataclass(frozen=True)
class Blacklist:
    """Compiled approval-blacklist: shell regexes + always-ask tool names."""

    shell_patterns: tuple[re.Pattern[str], ...]
    tools: frozenset[str]

    @classmethod
    def from_config(
        cls,
        shell_patterns: tuple[str, ...] = DEFAULT_SHELL_PATTERNS,
        tools: tuple[str, ...] = (),
    ) -> "Blacklist":
        """Compile the configured pattern strings (raises on an invalid regex)."""
        return cls(
            shell_patterns=tuple(re.compile(p) for p in shell_patterns),
            tools=frozenset(tools),
        )

    def match(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """The reason this call needs approval, or ``None`` if it runs freely."""
        if tool_name in self.tools:
            return f"{tool_name} is on the approval blacklist"
        if tool_name in COMMAND_TOOLS:
            command = str(tool_input.get("command", ""))
            for pattern in self.shell_patterns:
                if pattern.search(command):
                    return f"command matches blacklist pattern {pattern.pattern!r}"
        return None
