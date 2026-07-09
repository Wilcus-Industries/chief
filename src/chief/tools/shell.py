"""The owner's ``bash`` tool: a persistent per-task shell running on the host.

Host-native rework: the shell is a real subprocess of the core process — no sandbox
container, no RPC. The owner explicitly accepts that shell children inherit the full
process environment (including ``CLAUDE_CODE_OAUTH_TOKEN``); the approval blacklist
(:mod:`chief.gate.blacklist`) is what still gates the destructive shapes.

**Per-session shell.** Each session key gets a long-lived shell (``$SHELL`` if set,
else ``bash``, else ``zsh`` — macOS and Linux both covered) with ``cwd`` at the
workspace dir, so ``export``/``cd``/background jobs persist across commands within a
task. A command is first parse-checked (``-n``) so a malformed one (an unterminated
quote, an open here-doc) is rejected up front instead of hanging the shell; a clean one
is written to the shell's stdin followed by a unique **sentinel** marker carrying
``$?``, and the reader drains stdout/stderr until the sentinel, capturing the exit
code. A per-command **timeout** SIGINTs then respawns a hung shell (its state is lost —
an accepted degradation); if the shell dies mid-command the exit code is a distinct
non-zero, never a bogus 0; output past a **cap** is dropped and flagged ``truncated``.

:class:`ShellService` mirrors :class:`~chief.tools.google.GoogleService`: ``tasks.py``
wires it uniformly. One difference — the bash tool is built **per task** with the
task's key baked into its closure, because an in-process MCP handler receives only the
tool input (no caller context), and each task must address its **own** persistent
shell. The scheduler's bash fires share the same :meth:`ShellService.run` seam.
"""

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)

logger = logging.getLogger("chief.tools.shell")

#: Encoding for everything crossing the shell's pipes.
ENCODING = "utf-8"
#: The SDK names an in-process MCP tool ``mcp__<server>__<tool>``.
SERVER_NAME = "chief_shell"
TOOL_NAME = f"mcp__{SERVER_NAME}__bash"

#: The host shell's invariants, single-sourced here so the tool description and the
#: persona guidance (:mod:`chief.core.personas`) can't drift: it is the owner's real
#: machine, and steps chain in one command rather than across separate calls.
HOST_SHELL_CONTRACT = (
    "The shell runs directly on the owner's machine as their user, with their full "
    "environment. Chain steps with && in one command rather than relying on separate "
    "calls."
)

_BASH_DESCRIPTION = (
    "Run a shell command on the host. The working directory starts at the workspace "
    "scratch dir and the command has internet access. Shell state — environment "
    "variables, the current directory, background jobs — persists across calls within "
    f"this task (but not across a restart). {HOST_SHELL_CONTRACT} Commands run "
    "without approval unless they match the owner's approval blacklist (sudo, "
    "destructive operations, and similar)."
)

#: Exit code reported when a command is killed for exceeding its timeout (matches the
#: ``timeout(1)`` convention) so the model sees a distinct "this hung" signal.
TIMEOUT_EXIT_CODE = 124
#: Exit code when the shell process dies mid-command (EOF before the sentinel arrives):
#: a distinct non-zero signal so a crash isn't reported as a clean exit 0.
SHELL_DIED_EXIT_CODE = 137
#: Exit code for a command the pre-flight parse check rejects (the shell's own
#: convention for a syntax error). Returned without touching the persistent shell.
SYNTAX_ERROR_EXIT_CODE = 2
#: Generous per-line buffer for the subprocess stream readers (a single line longer
#: than this without a newline is beyond scope; the output cap is far smaller anyway).
_STREAM_LIMIT = 1 << 20

#: Bound on the pre-flight ``-n`` parse-check subprocess (LOW finding: every other
#: shell path is bounded by the command's own ``asyncio.wait_for`` timeout; this
#: throwaway parser had none, so a wedged parse process could hang a command forever).
#: The check itself does no I/O beyond ``DEVNULL`` stdin, so this only needs to be
#: generous enough for a slow host, not the command's own budget.
_SYNTAX_CHECK_TIMEOUT_SECONDS = 5.0


def resolve_shell() -> tuple[str, ...]:
    """The shell argv to spawn: ``$SHELL`` if set, else ``bash``, else ``zsh``.

    Covers Linux (bash default) and macOS (zsh default). The rc-suppressing flags keep
    the shell non-interactive and deterministic (``--norc --noprofile`` / ``-f``).

    Raises:
        RuntimeError: if no usable shell binary can be found.
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
    """Drop trailing newlines (the sentinel's leading ``\\n`` adds one; shell output
    conventionally ends in one too) — like bash ``$(...)`` capture; friendlier."""
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
    """How a stream drain finished: did the sentinel arrive, and the rc it carried.

    ``saw_sentinel=False`` means the stream hit EOF first — the shell died mid-command,
    so the rc is meaningless and the caller reports a failure rather than the default 0.
    """

    saw_sentinel: bool
    rc: int


async def _shell_syntax_error(argv: tuple[str, ...], command: str) -> str | None:
    """Parse ``command`` with ``<shell> -n`` (no execution); return the error or None.

    A command that leaves the shell mid-parse — an unterminated quote, a dangling
    here-doc — would, if written to the persistent shell, swallow the trailing sentinel
    and hang until the timeout (then respawn, losing session state). Catching it in a
    throwaway parser process lets a typo be rejected instantly. ``bash -n`` flags some
    incomplete constructs (an open here-doc) as a stderr *warning* with exit 0, so a
    non-empty diagnostic counts as a rejection too.

    A trailing **line continuation** (an odd run of unescaped backslashes at the very
    end) passes ``-n`` but would splice onto the appended sentinel line and corrupt
    both the output and the exit code — so it is rejected here explicitly.

    Bounded by :data:`_SYNTAX_CHECK_TIMEOUT_SECONDS`: a wedged parse process must not
    hang the command forever the way every other shell path is already guarded against
    (via the caller's own ``asyncio.wait_for``). A timeout here is treated as "no syntax
    error found" rather than a rejection — this check is a fast-fail heuristic, not a
    security boundary (the approval blacklist is), so the least-surprising behavior is
    to let the real command still attempt to run rather than block it on a check that
    itself misbehaved. Its own process group (mirroring :meth:`_Shell._terminate`) lets
    the timeout kill any grandchild the ``-c`` command spawned too — killing only the
    parser's own pid would leave a grandchild holding the stderr pipe open, hanging the
    ``communicate()`` cleanup until that grandchild exits on its own.
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


class _Shell:
    """One task's persistent shell subprocess, driven over its stdin pipe."""

    def __init__(self, argv: tuple[str, ...], sentinel: str, workdir: str) -> None:
        self._argv = argv
        self._sentinel = sentinel
        self._workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            # Full env inheritance is deliberate (host-native): the owner accepts that
            # shell children see the process env, OAuth token included.
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=self._workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group so a timeout can SIGINT/kill the whole job tree.
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
        return self._proc

    async def run(
        self, command: str, *, timeout: float, output_limit: int
    ) -> CommandResult:
        """Run ``command`` on the persistent shell; capture its output + exit code."""
        syntax_error = await _shell_syntax_error(self._argv, command)
        if syntax_error is not None:
            # Reject a malformed command up front, without writing it to the persistent
            # shell — doing so would swallow the sentinel and hang until the timeout,
            # then respawn, losing the whole session over a quoting typo.
            return CommandResult(
                "", syntax_error, SYNTAX_ERROR_EXIT_CODE, truncated=False
            )
        async with self._lock:  # one command at a time per shell
            proc = await self._ensure()
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None
            marker = self._sentinel
            # After the command: print the sentinel + $? on stdout (on its own line, via
            # a leading \n) and a bare sentinel on stderr, so each reader knows to stop.
            script = (
                f"{command}\n"
                f"__chief_rc=$?; "
                f"printf '\\n%s %s\\n' {marker} \"$__chief_rc\"; "
                f"printf '%s\\n' {marker} 1>&2\n"
            )
            proc.stdin.write(script.encode(ENCODING))
            await proc.stdin.drain()

            out = _Acc(output_limit)
            err = _Acc(output_limit)
            out_task = asyncio.create_task(self._drain(proc.stdout, out, parse_rc=True))
            err_task = asyncio.create_task(
                self._drain(proc.stderr, err, parse_rc=False)
            )
            try:
                out_end, _ = await asyncio.wait_for(
                    asyncio.gather(out_task, err_task), timeout=timeout
                )
            except TimeoutError:
                out_task.cancel()
                err_task.cancel()
                await asyncio.gather(out_task, err_task, return_exceptions=True)
                await self._terminate()  # SIGINT then kill; state is lost on respawn
                return CommandResult(
                    out.text(), err.text(), TIMEOUT_EXIT_CODE, truncated=True
                )
            truncated = out.truncated or err.truncated
            if not out_end.saw_sentinel:
                # Stdout reached EOF before the sentinel: the shell died mid-command.
                # Report a distinct non-zero code (not the default 0, which would mask
                # the failure) and force a fresh shell on the next command.
                await self._terminate()
                return CommandResult(
                    out.text(), err.text(), SHELL_DIED_EXIT_CODE, truncated=truncated
                )
            return CommandResult(
                out.text(), err.text(), out_end.rc, truncated=truncated
            )

    async def _drain(
        self, stream: asyncio.StreamReader, acc: _Acc, *, parse_rc: bool
    ) -> _DrainEnd:
        """Read lines into ``acc`` until the sentinel; report how the stream ended.

        ``parse_rc`` (stdout only) pulls ``$?`` off the sentinel line. EOF before the
        sentinel means the shell died mid-command — surfaced via ``saw_sentinel=False``
        so :meth:`run` reports a failure instead of a bogus exit 0.
        """
        marker = self._sentinel
        while True:
            line = await stream.readline()
            if not line:  # EOF — the shell died mid-command (no sentinel)
                return _DrainEnd(saw_sentinel=False, rc=0)
            text = line.decode(ENCODING, errors="replace")
            stripped = text.rstrip("\n")
            if stripped == marker or stripped.startswith(marker + " "):
                rc = 0
                if parse_rc:
                    parts = stripped.split(" ", 1)
                    if len(parts) == 2:
                        with contextlib.suppress(ValueError):
                            rc = int(parts[1])
                return _DrainEnd(saw_sentinel=True, rc=rc)
            acc.append(text)

    async def _terminate(self) -> None:
        """SIGINT the hung command, then hard-kill its process group; await the exit."""
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGINT)
            await asyncio.sleep(0.1)
            if proc.returncode is None:
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        self._proc = None  # forces a fresh shell on the next command

    async def close(self) -> None:
        await self._terminate()


class ShellHost:
    """Routes per-session commands to long-lived shells on the host."""

    def __init__(
        self,
        *,
        workdir: str,
        timeout: float,
        output_limit: int,
        argv: tuple[str, ...] | None = None,
        sentinel: str | None = None,
    ) -> None:
        self._workdir = workdir
        self._timeout = timeout
        self._output_limit = output_limit
        self._argv = argv or resolve_shell()
        #: Random per-process marker so a command's own output can't spoof the boundary.
        self._sentinel = sentinel or f"__CHIEF_SENTINEL_{secrets.token_hex(12)}__"
        self._shells: dict[str, _Shell] = {}

    async def execute(self, session_id: str, command: str) -> CommandResult:
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        shell = self._shells.get(session_id)
        if shell is None:
            shell = _Shell(self._argv, self._sentinel, self._workdir)
            self._shells[session_id] = shell
        return await shell.run(
            command, timeout=self._timeout, output_limit=self._output_limit
        )

    async def aclose(self) -> None:
        for shell in self._shells.values():
            await shell.close()
        self._shells.clear()


def format_result(result: dict[str, Any]) -> dict[str, Any]:
    """Render a shell result dict as an MCP tool result (text + is_error)."""
    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    exit_code = int(result.get("exit_code", 0))
    truncated = bool(result.get("truncated", False))
    segments: list[str] = []
    if stdout:
        segments.append(stdout)
    if stderr:
        segments.append(f"[stderr]\n{stderr}")
    if exit_code != 0:
        segments.append(f"[exit code {exit_code}]")
    if truncated:
        segments.append("[output truncated]")
    text = "\n".join(segments) if segments else "(no output)"
    return {"content": [{"type": "text", "text": text}], "is_error": exit_code != 0}


@dataclass
class ShellService:
    """Workspace + limits for the host shell; builds the per-task bash tool.

    Owns one :class:`ShellHost` (created lazily so the shell binary is only resolved
    when a command actually runs), shared by every task's tool closure and by the
    scheduler's bash fires (:meth:`run` is the single seam).
    """

    workspace_dir: str
    timeout_seconds: float
    output_limit: int
    server_name: str = SERVER_NAME
    _host: ShellHost | None = field(default=None, init=False, repr=False)

    @property
    def tool_name(self) -> str:
        """The SDK-qualified ``mcp__chief_shell__bash`` name (gate/policy wiring)."""
        return f"mcp__{self.server_name}__bash"

    def _ensure_host(self) -> ShellHost:
        if self._host is None:
            self._host = ShellHost(
                workdir=self.workspace_dir,
                timeout=self.timeout_seconds,
                output_limit=self.output_limit,
            )
        return self._host

    async def run(self, session_id: str, command: str) -> dict[str, Any]:
        """Run one command on ``session_id``'s persistent shell; return its dict."""
        result = await self._ensure_host().execute(session_id, command)
        return result.as_dict()

    async def aclose(self) -> None:
        """Terminate every live shell (clean teardown)."""
        if self._host is not None:
            await self._host.aclose()

    def _build_bash_tool(self, session_key: str) -> InProcessTool:
        @tool("bash", _BASH_DESCRIPTION, {"command": str})
        async def bash(args: dict[str, Any]) -> dict[str, Any]:
            command = str(args.get("command", ""))
            try:
                result = await self.run(session_key, command)
            except Exception as exc:  # shell spawn failed — surface, don't crash
                return {
                    "content": [
                        {"type": "text", "text": f"shell unavailable: {exc}"}
                    ],
                    "is_error": True,
                }
            return format_result(result)

        return bash

    def server_config(self, *, session_key: str) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for this task's bash tool."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_bash_tool(session_key)]
        )
