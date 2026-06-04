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
within a task. A command is written to the shell's stdin followed by a unique
**sentinel** marker carrying ``$?``; the server streams stdout/stderr until the
sentinel, capturing the exit code. A per-command **timeout** SIGINTs then respawns a
hung shell
(its state is lost — an accepted degradation); output past a **cap** is dropped and
flagged ``truncated``.

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

    def __init__(self, limit: int, *, want_rc: bool) -> None:
        self._limit = limit
        self._want_rc = want_rc
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False
        self.rc = 0
        self.done = False

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

            out = _Acc(output_limit, want_rc=True)
            err = _Acc(output_limit, want_rc=False)
            out_task = asyncio.create_task(self._drain(proc.stdout, out))
            err_task = asyncio.create_task(self._drain(proc.stderr, err))
            try:
                await asyncio.wait_for(
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
            return CommandResult(
                out.text(), err.text(), out.rc, truncated=out.truncated or err.truncated
            )

    async def _drain(self, stream: asyncio.StreamReader, acc: _Acc) -> None:
        """Read lines into ``acc`` until the sentinel (parsing ``$?`` on stdout)."""
        marker = self._sentinel
        while True:
            line = await stream.readline()
            if not line:  # EOF — the shell died mid-command
                acc.done = True
                return
            text = line.decode(ENCODING, errors="replace")
            stripped = text.rstrip("\n")
            if stripped == marker or stripped.startswith(marker + " "):
                if acc._want_rc:
                    parts = stripped.split(" ", 1)
                    if len(parts) == 2:
                        with contextlib.suppress(ValueError):
                            acc.rc = int(parts[1])
                acc.done = True
                return
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
