"""Stateless framing primitives for the host shell driver
(:mod:`chief.tools.shell.host`).

The wire protocol between the daemon and a persistent shell: which shell to spawn
(:func:`resolve_shell`), the exit-code conventions, the output accumulator/cap
(:class:`_Acc`), the drain-end signal (:class:`_DrainEnd`), and the ``-n`` pre-flight
parse check (:func:`_shell_syntax_error`). No process state lives here — that is
:class:`~chief.tools.shell.host._Shell`.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("chief.tools.shell.frame")

#: Encoding for everything crossing the shell's pipes.
ENCODING = "utf-8"
#: Killed for exceeding its timeout (matches ``timeout(1)``) — a distinct "hung" signal.
TIMEOUT_EXIT_CODE = 124
#: Shell died mid-command (EOF before the sentinel): distinct non-zero, never a bogus 0.
SHELL_DIED_EXIT_CODE = 137
#: Rejected by the pre-flight parse check (the shell's own syntax-error convention).
SYNTAX_ERROR_EXIT_CODE = 2
#: Per-line buffer for the stream readers; the output cap is far smaller anyway.
STREAM_LIMIT = 1 << 20
#: Bound on the ``-n`` parse-check subprocess so a wedged parser can't hang a command
#: forever the way every other path is guarded by the caller's own ``wait_for``.
_SYNTAX_CHECK_TIMEOUT_SECONDS = 5.0


def resolve_shell() -> tuple[str, ...]:
    """The shell argv to spawn: ``$SHELL`` if set, else ``bash``, else ``zsh``.

    Covers Linux (bash) and macOS (zsh). The rc-suppressing flags keep it
    non-interactive and deterministic. Raises ``RuntimeError`` if none is found.
    """
    candidate = os.environ.get("SHELL", "").strip()
    path = candidate if candidate and Path(candidate).is_file() else None
    if path is None:
        path = shutil.which("bash") or shutil.which("zsh")
    if path is None:
        raise RuntimeError("no usable shell found — set $SHELL or install bash/zsh")
    name = Path(path).name
    if name == "bash":
        return (path, "--norc", "--noprofile")
    if name == "zsh":
        return (path, "-f")
    return (path,)


@dataclass
class CommandResult:
    """One command's captured output + exit status."""

    stdout: str
    stderr: str
    exit_code: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "truncated": self.truncated,
        }


def _rstrip_newlines(text: str) -> str:
    """Drop trailing newlines (like bash ``$(...)`` capture); the sentinel adds one."""
    return text.rstrip("\n")


class _Acc:
    """Accumulates one stream's output up to a cap, then flags truncation."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def text(self) -> str:
        return _rstrip_newlines("".join(self.parts))

    def append(self, chunk: str) -> None:
        if self.size >= self._limit:
            self.truncated = True
            return
        room = self._limit - self.size
        take = chunk[:room]
        self.parts.append(take)
        self.size += len(take)
        if len(take) < len(chunk):
            self.truncated = True


@dataclass
class _DrainEnd:
    """How a drain finished: ``saw_sentinel=False`` means EOF first (shell died)."""

    saw_sentinel: bool
    rc: int


async def _shell_syntax_error(argv: tuple[str, ...], command: str) -> str | None:
    """Parse ``command`` with ``<shell> -n`` (no execution); return the error or None.

    A command that leaves the shell mid-parse (unterminated quote, open here-doc) would
    swallow the sentinel and hang until the timeout — a throwaway parser rejects it
    instantly. ``bash -n`` warns on some incomplete constructs with exit 0, so a
    non-empty diagnostic counts too. A trailing line continuation passes ``-n`` but
    would splice onto the sentinel line, so it is rejected explicitly.

    Bounded by :data:`_SYNTAX_CHECK_TIMEOUT_SECONDS`; a timeout is treated as "no error"
    (this is a fast-fail heuristic, not a security boundary — the gate is). Its own
    process group lets the timeout kill any grandchild the ``-c`` spawned.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        "-n",
        "-c",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, raw = await asyncio.wait_for(
            proc.communicate(), timeout=_SYNTAX_CHECK_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("shell syntax pre-check timed out; letting the command proceed")
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    diagnostic = raw.decode(ENCODING, errors="replace").strip()
    if proc.returncode != 0 or diagnostic:
        return diagnostic or "syntax error"
    if (len(command) - len(command.rstrip("\\"))) % 2 == 1:
        return "syntax error: command ends in a dangling line continuation (\\)"
    return None
