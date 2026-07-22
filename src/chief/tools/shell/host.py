"""Stateful host shell drivers: the persistent subprocess and its per-thread router.

Host-native — a real subprocess of the daemon, no sandbox, no RPC. :class:`_Shell` owns
one long-lived shell whose ``export``/``cd``/jobs persist across commands; each command
is parse-checked (:func:`~chief.tools.shell.frame._shell_syntax_error`) then framed with
a unique sentinel carrying ``$?`` so the readers know where output ends and can recover
the exit code. A per-command timeout SIGINTs then respawns a hung shell (state lost); a
shell that dies mid-command yields a distinct non-zero code, never a bogus 0.
:class:`ShellHost` keeps one shell per thread key. Framing primitives live in
:mod:`chief.tools.shell.frame`; the tool surface in :mod:`chief.tools.shell.service`.
"""

import asyncio
import contextlib
import os
import secrets
import signal
from pathlib import Path

from chief.tools.shell.frame import (
    ENCODING,
    SHELL_DIED_EXIT_CODE,
    STREAM_LIMIT,
    SYNTAX_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    CommandResult,
    _Acc,
    _DrainEnd,
    _shell_syntax_error,
    resolve_shell,
)


class _Shell:
    """One thread's persistent shell subprocess, driven over its stdin pipe."""

    def __init__(self, argv: tuple[str, ...], sentinel: str, workdir: str) -> None:
        self._argv = argv
        self._sentinel = sentinel
        self._workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            # Full env inheritance is deliberate (host-native, owner-accepted).
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=self._workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own group so a timeout kills the whole tree
                limit=STREAM_LIMIT,
            )
        return self._proc

    async def run(
        self, command: str, *, timeout: float, output_limit: int
    ) -> CommandResult:
        """Run ``command`` on the persistent shell; capture its output + exit code."""
        syntax_error = await _shell_syntax_error(self._argv, command)
        if syntax_error is not None:
            # Reject up front without touching the shell — writing it would swallow the
            # sentinel and hang until the timeout, losing the session over a typo.
            return CommandResult(
                "", syntax_error, SYNTAX_ERROR_EXIT_CODE, truncated=False
            )
        async with self._lock:  # one command at a time per shell
            proc = await self._ensure()
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None
            marker = self._sentinel
            # After the command: sentinel + $? on stdout (own line, leading \n) and a
            # bare sentinel on stderr, so each reader knows to stop.
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
                # EOF before the sentinel: the shell died mid-command. Report a distinct
                # code (not a masking 0) and force a fresh shell next time.
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

        ``parse_rc`` (stdout) pulls ``$?`` off the sentinel line. EOF first means the
        shell died mid-command — surfaced via ``saw_sentinel=False``.
        """
        marker = self._sentinel
        while True:
            line = await stream.readline()
            if not line:  # EOF — the shell died mid-command (no sentinel)
                return _DrainEnd(saw_sentinel=False, rc=0)
            stripped = line.decode(ENCODING, errors="replace").rstrip("\n")
            if stripped == marker or stripped.startswith(marker + " "):
                rc = 0
                if parse_rc:
                    parts = stripped.split(" ", 1)
                    if len(parts) == 2:
                        with contextlib.suppress(ValueError):
                            rc = int(parts[1])
                return _DrainEnd(saw_sentinel=True, rc=rc)
            acc.append(line.decode(ENCODING, errors="replace"))

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
    """Routes per-thread commands to long-lived shells on the host."""

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
        #: Random per-process marker so a command's output can't spoof the boundary.
        self._sentinel = sentinel or f"__CHIEF_SENTINEL_{secrets.token_hex(12)}__"
        self._shells: dict[str, _Shell] = {}

    async def execute(
        self, thread_key: str, command: str, *, timeout: float | None = None
    ) -> CommandResult:
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        shell = self._shells.get(thread_key)
        if shell is None:
            shell = _Shell(self._argv, self._sentinel, self._workdir)
            self._shells[thread_key] = shell
        return await shell.run(
            command,
            timeout=self._timeout if timeout is None else timeout,
            output_limit=self._output_limit,
        )

    async def aclose(self) -> None:
        for shell in self._shells.values():
            await shell.close()
        self._shells.clear()
