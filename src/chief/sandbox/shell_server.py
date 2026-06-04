"""Stdlib asyncio shell server for the sandbox container (M7).

Runs in the **secret-free** sandbox container and is the only thing core talks to over
the internal compose network. Pure stdlib — no MCP, no starlette/uvicorn — so the image
stays tiny and its attack surface small (DESIGN: sandbox worker).

**Protocol** — newline-delimited JSON over TCP. The client sends one request object per
line::

    {"session_id": "<task key>", "command": "<bash>"}

and the server replies with one response line::

    {"stdout": "...", "stderr": "...", "exit_code": 0, "truncated": false}

**Per-session shell.** Each ``session_id`` gets a long-lived, non-login ``/bin/bash``
with ``cwd=/workspace``, so ``export``/``cd``/background jobs persist across commands
within a task. A command is first parse-checked with ``bash -n`` so a malformed one (an
unterminated quote, an open here-doc) is rejected up front instead of hanging the shell;
a clean one is written to the shell's stdin followed by a unique **sentinel** marker
carrying ``$?``, and the server streams stdout/stderr until the sentinel, capturing the
exit code. A per-command **timeout** SIGINTs then respawns a hung shell (its state is
lost — an accepted degradation); if the shell dies mid-command the exit code is a
distinct non-zero, never a bogus 0; output past a **cap** is dropped and flagged
``truncated``.

The bash process is ephemeral: a sandbox restart drops every shell, and the task resumes
with a fresh one. The wire format lives here so :mod:`chief.tools.shell` (the core-side
client) can import the constants and stay byte-compatible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("chief.sandbox.shell_server")

#: Wire encoding for the newline-delimited JSON protocol.
ENCODING = "utf-8"
#: Where every shell starts and is confined to (the shared workspace volume).
WORKDIR = "/workspace"
#: Defaults; the container overrides these from the environment (see :func:`main`).
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 — internal compose network only, no host ports
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120.0
DEFAULT_OUTPUT_LIMIT = 64_000
#: Exit code reported when a command is killed for exceeding its timeout (matches the
#: ``timeout(1)`` convention) so the model sees a distinct "this hung" signal.
TIMEOUT_EXIT_CODE = 124
#: Exit code when the shell process dies mid-command (EOF before the sentinel arrives):
#: a distinct non-zero signal so a crash isn't reported as a clean exit 0.
SHELL_DIED_EXIT_CODE = 137
#: Exit code for a command the pre-flight parse check rejects (bash's own convention for
#: a syntax error). Returned without ever touching the persistent shell.
SYNTAX_ERROR_EXIT_CODE = 2
#: Generous per-line buffer for the subprocess stream readers (a single line longer than
#: this without a newline is beyond scope; the output cap is far smaller anyway).
_STREAM_LIMIT = 1 << 20


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


async def _bash_syntax_error(command: str) -> str | None:
    """Parse ``command`` with ``bash -n`` (no execution); return the error, or ``None``.

    A command that leaves bash mid-parse — an unterminated quote, a dangling here-doc —
    would, if written to the persistent shell, swallow the trailing sentinel and hang
    until the timeout (then respawn, losing session state). Catching it in a throwaway
    parser process lets the shell reject a typo instantly without disturbing it.
    ``bash -n`` flags some incomplete constructs (an open here-doc) as a stderr
    *warning* with exit 0, so a non-empty diagnostic counts as a rejection too.
    """
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "--norc",
        "--noprofile",
        "-n",
        "-c",
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, raw = await proc.communicate()
    diagnostic = raw.decode(ENCODING, errors="replace").strip()
    if proc.returncode == 0 and not diagnostic:
        return None
    return diagnostic or "syntax error"


class _Shell:
    """One task's persistent bash subprocess, driven over its stdin pipe."""

    def __init__(self, sentinel: str, workdir: str) -> None:
        self._sentinel = sentinel
        self._workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--norc",
                "--noprofile",
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
        syntax_error = await _bash_syntax_error(command)
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


class ShellServer:
    """Routes per-session commands to long-lived shells and frames the JSON protocol."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        workdir: str = WORKDIR,
        sentinel: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._output_limit = output_limit
        self._workdir = workdir
        #: Random per-process marker so a command's own output can't spoof the boundary.
        self._sentinel = sentinel or f"__CHIEF_SENTINEL_{secrets.token_hex(12)}__"
        self._shells: dict[str, _Shell] = {}

    async def execute(self, session_id: str, command: str) -> CommandResult:
        shell = self._shells.get(session_id)
        if shell is None:
            shell = _Shell(self._sentinel, self._workdir)
            self._shells[session_id] = shell
        return await shell.run(
            command, timeout=self._timeout, output_limit=self._output_limit
        )

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One client connection: a stream of request lines → response lines."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = json.loads(line)
                    session_id = str(req["session_id"])
                    command = str(req["command"])
                except (ValueError, KeyError, TypeError):
                    writer.write(self._error("malformed request"))
                    await writer.drain()
                    continue
                try:
                    result = await self.execute(session_id, command)
                    payload = result.as_dict()
                except Exception:  # never let one command take the server down
                    logger.exception("command execution failed")
                    writer.write(self._error("command execution failed"))
                    await writer.drain()
                    continue
                writer.write((json.dumps(payload) + "\n").encode(ENCODING))
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _error(message: str) -> bytes:
        payload = {
            "stdout": "",
            "stderr": message,
            "exit_code": 1,
            "truncated": False,
        }
        return (json.dumps(payload) + "\n").encode(ENCODING)

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self.handle, host, port)
        addr = ", ".join(str(s.getsockname()) for s in (server.sockets or ()))
        logger.info("sandbox shell server listening on %s", addr)
        async with server:
            await server.serve_forever()

    async def aclose(self) -> None:
        for shell in self._shells.values():
            await shell.close()
        self._shells.clear()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def _main() -> None:
    host = os.environ.get("SANDBOX_HOST") or DEFAULT_HOST
    port = _env_int("SANDBOX_PORT", DEFAULT_PORT)
    server = ShellServer(
        timeout=_env_float("SHELL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT),
        output_limit=_env_int("SHELL_OUTPUT_LIMIT", DEFAULT_OUTPUT_LIMIT),
    )
    await server.serve(host, port)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
