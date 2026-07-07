"""The approval blacklist — what still needs a card under the default-allow gate.

The host-native posture inverts the old default-ask gate for the owner: effectful tool
calls run freely, and only a call matching this blacklist raises an approval card.
Two kinds of entry:

- **Shell patterns** — regexes matched against a command tool's (see
  :data:`~chief.gate.policy.COMMAND_TOOLS`) command string. The defaults cover the
  classically destructive shapes: sudo/doas, ``rm -rf`` on a root or critical system
  dir, writing to raw devices, shutdown/reboot, killing PID 1, pipe/redirect-to-shell
  installers, force-pushing main, ``chmod -R 777``, and global package installs.
- **Tool names** — whole tools that always ask, regardless of input.

A match means ASK (a card), never DENY — over-broad patterns cost one tap, so the
defaults lean broad. Both lists are configurable in ``config.yaml``
(``blacklist_shell_patterns`` / ``blacklist_tools``).

**Honesty about what this is (and isn't).** This blacklist is a defense-in-depth
footgun-catcher, not a security boundary — it exists to catch *accidental* and
low-effort destructive commands, not a determined adversary who controls the command
string. Canonicalizing the command before matching (below) closes the cheap, common
evasions (quoting, escaping) but does **not** stop someone who can construct arbitrary
shell: variable indirection (``x=sudo; $x rm -rf /``), ``base64 -d | sh``, subshells
(``$(...)``), or an alternate binary/path the patterns don't name all remain possible
by design. Defense against *injected* (attacker-controlled) instructions lives in the
untrusted-content screening layer (see DESIGN.md's security model), not here. Don't
mistake a green run through this module for "the command was safe" — it only means
"this command didn't trip the speed bump."

**Canonicalize, then match.** Matching the raw command string with ``re.search`` is
trivially evaded by quoting/escaping a keyword apart (``su''do``, ``s\\udo``) or by
quoting a target so a boundary the regex expects (a space before ``/``) never
literally appears (``rm -rf "/"``). To close that off, every pattern is checked
against the command re-canonicalized through :mod:`shlex`
(:func:`chief.gate.policy.canonical_command` — reused, not reinvented): the command is
tokenized and rejoined, which collapses quoting/escaping tricks back to their plain
form. Patterns are *also* checked against the raw string, because canonicalization
requotes shell operators it doesn't understand (``|``, ``&&``, ``>``) as literal
tokens (``shlex.join`` emits ``curl url '|' bash``), which would break a pipe/redirect
pattern that expects the bare operator character. Checking both is a superset, never
narrower than either alone, so it can only catch more, not less.

**Unparseable commands fail toward ASK.** A command shlex can't tokenize (e.g.
unbalanced quotes) skips canonicalization entirely — rather than silently falling back
to a raw-string match that a crafted unbalanced-quote string could bypass, an
unparseable command is itself treated as a match (card raised, reason explains why).
"""

import re
from dataclasses import dataclass
from typing import Any

from .policy import COMMAND_TOOLS, canonical_command, parse_command

#: Default shell-command patterns that require owner approval. Each is a regex checked
#: against both the shlex-canonicalized and the raw command string (see the module
#: doc). Deliberately broad: a false positive costs one approval tap, a false negative
#: costs a machine.
DEFAULT_SHELL_PATTERNS: tuple[str, ...] = (
    # Privilege escalation.
    r"\b(sudo|doas)\b",
    # rm with a recursive flag (short -r/-R bundle, or --recursive) aimed at a
    # filesystem root, $HOME, ~, a glob-root (/*), or a critical system dir. The
    # target group tolerates an optional single-quote wrap because canonicalization
    # requotes args shlex considers shell-special (~, $HOME, /*) — see module doc.
    r"\brm\b(?=.*\s(?:-\w*[rR]\w*|--recursive)\b)\s.*\s"
    r"'?(?:~/?|\$HOME/?|/etc/?|/usr/?|/boot/?|/var/?|/\*+|/)'?(?=\s|$|;|&&)",
    # Filesystem creation / raw writes to device nodes.
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^|;&]*\bof=/dev/",
    # Host power state.
    r"\b(shutdown|reboot|poweroff|halt)\b",
    # Killing init, by signal number or name (-9, -KILL/-SIGKILL, -s KILL/SIGKILL).
    r"\bkill\b[^|;&]*(?:-9|-(?:SIG)?KILL|-s\s+(?:SIG)?KILL)\s+1\b",
    # Pipe-to-shell-or-interpreter installers (curl ... | sh and friends). Only
    # reliably present in the raw string — canonicalization requotes a bare "|" as a
    # literal token, which would defeat this pattern (see module doc).
    r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?"
    r"(python3?|perl|ruby|node|(ba|z|da|fi|k)?sh)\b",
    # Download-to-file then execute in a separate step (curl ... > f && sh f). Same
    # raw-string requirement as the pipe form above.
    r"\b(curl|wget)\b[^|;&\n]*>\s*\S+[^|;&\n]*&&\s*(sudo\s+)?"
    r"(python3?|perl|ruby|node|(ba|z|da|fi|k)?sh)\b",
    # Force-pushing a protected branch: --force/-f, or a +refspec force prefix.
    r"\bgit\s+push\b(?=.*(?:--force\b|\s-f\b|\s\+\w))(?=.*\b(?:main|master)\b)",
    # World-writable recursive chmod: -R/--recursive plus 777 (any number of leading
    # zeros, e.g. 0777).
    r"\bchmod\b(?=.*\s(?:-\w*[rR]\w*|--recursive)\b)(?=.*\b0*777\b)",
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
            try:
                parse_command(command)
            except ValueError:
                # Can't canonicalize what shlex can't tokenize. Fail safe toward ASK
                # rather than silently falling through to a bypassable raw match.
                return "command could not be parsed (unbalanced quotes?) — asking"
            canonical = canonical_command(command)
            for pattern in self.shell_patterns:
                if pattern.search(canonical) or pattern.search(command):
                    return f"command matches blacklist pattern {pattern.pattern!r}"
        return None
