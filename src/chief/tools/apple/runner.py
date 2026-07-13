"""The Apple family's scripting-runner subprocess seam (#155).

Everything the Apple tool family does drives a macOS child process through this one
class: AppleScript/JXA via ``osascript``, the Shortcuts CLI, the Messages store via the
``sqlite3`` CLI, and the small system utilities (``pbcopy``/``pbpaste``/
``screencapture``). The subprocess boundary is the family's contract **and its test
seam**: CI (Linux) fakes the runner at exactly this boundary (argv + stdin in,
:class:`ScriptResult` out) and asserts the exact generated invocations, while the
opt-in on-Mac live suite runs the same calls for real.

Owner data always travels as **argv** (or stdin), never spliced into script text — JXA
scripts are fixed constants using ``function run(argv)``, so there is no quoting or
injection surface in the generated code. Children are spawned exec-style (argv list,
no shell), and binaries are addressed by absolute path (the macOS system locations) so
a ``$PATH`` hijack can't swap one out.

Subprocess handling mirrors :mod:`chief.tools.shell`: each call is bounded by a
timeout (a hung child is killed by process group, reported as exit
:data:`TIMEOUT_EXIT_CODE`), and captured output is capped.
"""

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("chief.tools.apple.runner")

ENCODING = "utf-8"

#: Exit code reported when a script is killed for exceeding its timeout (matches the
#: ``timeout(1)`` convention, same as :mod:`chief.tools.shell`).
TIMEOUT_EXIT_CODE = 124
#: Exit code reported when the binary itself is missing (the shell convention).
NOT_FOUND_EXIT_CODE = 127

#: macOS system binaries, by absolute path (see the module doc).
OSASCRIPT_PATH = "/usr/bin/osascript"
SHORTCUTS_PATH = "/usr/bin/shortcuts"
SQLITE3_PATH = "/usr/bin/sqlite3"
PBCOPY_PATH = "/usr/bin/pbcopy"
PBPASTE_PATH = "/usr/bin/pbpaste"
SCREENCAPTURE_PATH = "/usr/sbin/screencapture"

#: Captured macOS permission-denial shapes (see ``tests/test_apple_runner.py``; the
#: live suite re-validates them on the real Mac so they can't drift):
#: ``-1743`` is the Automation (Apple events) TCC denial from osascript; the sqlite
#: shapes are chat.db without Full Disk Access (SQLITE_CANTOPEN / SQLITE_AUTH).
_DENIED_MARKERS: tuple[str, ...] = (
    "Not authorized to send Apple events",
    "(-1743)",
    "authorization denied",
    "unable to open database",
)


@dataclass(frozen=True)
class ScriptResult:
    """One child process's captured output + exit status."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """True when the child exited 0 within its timeout."""
        return self.exit_code == 0 and not self.timed_out


def is_permission_denied(result: ScriptResult) -> bool:
    """True when a failure looks like a missing macOS TCC grant.

    Keyed off the captured denial shapes in :data:`_DENIED_MARKERS`; a successful
    result is never a denial. Used by tool handlers to route the owner to the
    permissions doctor instead of surfacing a raw scripting error.
    """
    if result.ok:
        return False
    text = f"{result.stderr}\n{result.stdout}"
    return any(marker in text for marker in _DENIED_MARKERS)


def text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """An MCP tool result carrying one text block (mirrors chief's other tools)."""
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def script_error_result(action: str, result: ScriptResult) -> dict[str, Any]:
    """Render a failed :class:`ScriptResult` as a helpful MCP error result.

    A permission denial points the owner at the ``check_apple_health`` doctor tool
    (which carries the exact System Settings walk-through) instead of dumping a raw
    osascript/sqlite error; timeouts and plain failures surface their diagnostic.
    """
    if is_permission_denied(result):
        detail = result.stderr.strip() or result.stdout.strip()
        return text_result(
            f"Could not {action}: macOS denied the required permission. Run the "
            "check_apple_health tool for the exact System Settings steps to grant "
            f"it. (macOS said: {detail})",
            is_error=True,
        )
    detail = result.stderr.strip() or result.stdout.strip()
    if not detail:
        detail = f"exit code {result.exit_code}"
    return text_result(f"Could not {action}: {detail}", is_error=True)


@dataclass
class ScriptRunner:
    """Runs the macOS automation binaries as bounded child processes.

    ``timeout`` bounds one call (a hung child is killed by process group);
    ``output_limit`` caps each captured stream. The ``*_path`` fields exist so tests
    can point a call at a stub executable — production always uses the macOS system
    paths.
    """

    timeout: float = 30.0
    output_limit: int = 200_000
    osascript_path: str = OSASCRIPT_PATH
    shortcuts_path: str = SHORTCUTS_PATH
    sqlite3_path: str = SQLITE3_PATH
    pbcopy_path: str = PBCOPY_PATH
    pbpaste_path: str = PBPASTE_PATH
    screencapture_path: str = SCREENCAPTURE_PATH

    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None = None
    ) -> ScriptResult:
        """Run ``argv`` to completion; capture output, bounded by the timeout.

        Never raises for an unrunnable or misbehaving child — a missing binary, a
        timeout, and a non-zero exit all come back as a :class:`ScriptResult` so
        tool handlers degrade to a readable error instead of crashing the turn.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group so a timeout can kill the whole job tree.
                start_new_session=True,
            )
        except OSError as exc:
            return ScriptResult(
                "",
                f"{argv[0]}: not found or not runnable ({exc})",
                NOT_FOUND_EXIT_CODE,
            )
        try:
            out_raw, err_raw = await asyncio.wait_for(
                proc.communicate(stdin), timeout=self.timeout
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            logger.warning("apple script call timed out: %s", argv[0])
            return ScriptResult(
                "",
                f"timed out after {self.timeout:g}s",
                TIMEOUT_EXIT_CODE,
                timed_out=True,
            )
        stdout, out_truncated = self._cap(out_raw)
        stderr, err_truncated = self._cap(err_raw)
        exit_code = proc.returncode if proc.returncode is not None else 0
        return ScriptResult(
            stdout, stderr, exit_code, truncated=out_truncated or err_truncated
        )

    def _cap(self, raw: bytes) -> tuple[str, bool]:
        text = raw.decode(ENCODING, errors="replace")
        if len(text) <= self.output_limit:
            return text, False
        return text[: self.output_limit], True

    async def run_jxa(
        self, script: str, args: Sequence[str] = ()
    ) -> ScriptResult:
        """Run a fixed JXA script via osascript, passing owner data as argv."""
        return await self.run(
            [self.osascript_path, "-l", "JavaScript", "-e", script, *args]
        )

    async def run_shortcuts(
        self, args: Sequence[str], *, stdin: bytes | None = None
    ) -> ScriptResult:
        """Run the Shortcuts CLI (``shortcuts list`` / ``shortcuts run …``)."""
        return await self.run([self.shortcuts_path, *args], stdin=stdin)

    async def run_sqlite(self, db_path: str, query: str) -> ScriptResult:
        """Run one read-only JSON-mode query via the sqlite3 CLI.

        ``-readonly`` is load-bearing: the Messages store is read-only here by
        design (writing belongs to the iMessage adapter PRD, not this family).
        """
        return await self.run(
            [self.sqlite3_path, "-readonly", "-json", db_path, query]
        )
