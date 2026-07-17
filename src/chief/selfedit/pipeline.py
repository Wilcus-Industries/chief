"""The guarded self-edit pipeline.

Edits land on a scratch branch, the full done-check runs there, and only a
green check merges back and restarts the daemon. A marker file survives the
restart so a boot failure can roll back to the recorded commit (see
``recovery.py``). Every step is audited.

Subprocesses run with explicit argument lists (never a shell), so file
contents and rationales cannot inject commands.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from chief.audit import AuditLog
from chief.selfedit.recovery import MARKER_NAME

logger = logging.getLogger(__name__)

# The project done-check (CLAUDE.md); injectable so tests use a fast stand-in.
DEFAULT_CHECKS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "mypy", "."),
)

FORBIDDEN_PREFIXES = ("secrets", ".git", "data")


class SelfEditPipeline:
    """Applies agent-proposed edits under the done-check seatbelt."""

    def __init__(
        self,
        repo_root: Path,
        audit: AuditLog,
        restart: Callable[[], None],
        checks: tuple[tuple[str, ...], ...] = DEFAULT_CHECKS,
    ) -> None:
        self._root = repo_root
        self._audit = audit
        self._restart = restart
        self._checks = checks

    async def apply(self, files: dict[str, str], rationale: str) -> str:
        """Run the pipeline for a set of file edits; returns a result string.

        On green: edits are merged, a rollback marker is written, and the
        daemon restarts into the new code. On red: everything is reverted
        and the check output comes back for the model to fix.
        """
        for path in files:
            if reason := _reject_path(path):
                return f"error: {reason}"

        async def write() -> None:
            for path, content in files.items():
                target = self._root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

        return await self._guarded(write, rationale, list(files))

    async def install(
        self, scripts: list[Path], env: dict[str, str], rationale: str
    ) -> str:
        """Run package install scripts, in order, under the same seatbelt.

        Each script is a package's own ``install.sh`` — trusted, deterministic
        bytes (copy skill dirs verbatim, set config keys, wire MCP). They run
        as one guarded mutation: a single done-check, merge, and restart, so
        the whole install is one approval and rolls back as a unit (issue
        #185). ``env`` (e.g. ``IMESSAGE_HANDLES``) is passed to every script.
        """

        async def run() -> None:
            for script in scripts:
                await self._run_installer(script, env)

        return await self._guarded(run, rationale, [str(s) for s in scripts])

    async def _guarded(
        self,
        mutate: Callable[[], Awaitable[None]],
        rationale: str,
        audit_files: list[str],
    ) -> str:
        """The shared seatbelt: branch, mutate, done-check, merge or revert."""
        if await self._dirty():
            return "error: working tree is dirty; refusing to self-edit"
        base = (await self._git("rev-parse", "HEAD")).strip()
        branch = f"selfedit-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        await self._git("checkout", "-b", branch)
        try:
            await mutate()
            await self._git("add", "-A")
            await self._git("commit", "--allow-empty", "-m", f"self-edit: {rationale}")
            failure = await self._run_checks()
        except Exception:
            await self._abandon(branch, base)
            raise
        if failure is not None:
            await self._abandon(branch, base)
            self._audit.record("self_edit", outcome="check_failed", files=audit_files)
            return f"error: done-check failed, edit reverted\n{failure}"
        await self._git("checkout", "-")
        await self._git("merge", "--ff-only", branch)
        await self._git("branch", "-d", branch)
        marker = {"rollback_to": base, "rationale": rationale}
        (self._root / MARKER_NAME).write_text(json.dumps(marker))
        self._audit.record(
            "self_edit", outcome="merged", files=audit_files, rationale=rationale
        )
        logger.info("self-edit merged (%s); restarting", rationale)
        self._restart()
        return "self-edit applied; restarting into the new code"

    async def _run_installer(self, script: Path, env: dict[str, str]) -> None:
        """Run one install.sh with the given params (arg list, no shell)."""
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(self._root / script),
            cwd=self._root,
            env={**os.environ, **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            tail = output.decode(errors="replace")[-4000:]
            raise RuntimeError(f"installer {script} failed:\n{tail}")

    async def _run_checks(self) -> str | None:
        """Run every check command; return combined output on first failure."""
        for cmd in self._checks:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            if process.returncode != 0:
                return f"$ {' '.join(cmd)}\n{output.decode(errors='replace')[-4000:]}"
        return None

    async def _abandon(self, branch: str, base: str) -> None:
        await self._git("checkout", "-")
        await self._git("branch", "-D", branch)
        await self._git("reset", "--hard", base)
        # A mutation that raised mid-run (e.g. an installer that copied a
        # skill, then aborted) leaves untracked files reset --hard won't
        # touch. The seatbelt entry guaranteed a clean tree, so clean -fd
        # only removes those; -x is omitted so gitignored config/data stay.
        await self._git("clean", "-fd")

    async def _dirty(self) -> bool:
        # The rollback marker is the pipeline's own runtime state, written
        # post-merge and normally cleared on the next healthy boot. If it
        # lingers (recovery skipped, crash), it must not count as "dirty" and
        # refuse every future self-edit — the state that stranded the mini.
        # `.gitignore` also lists it so human `git status` stays clean; this
        # filter makes the guard robust even without that entry.
        lines = (await self._git("status", "--porcelain")).splitlines()
        return any(line[3:].strip() != MARKER_NAME for line in lines if line.strip())

    async def _git(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {output.decode(errors='replace')}"
            )
        return output.decode()


def _reject_path(path: str) -> str | None:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        return f"path escapes the harness: {path}"
    if p.parts and p.parts[0] in FORBIDDEN_PREFIXES:
        return f"path is off-limits to self-edit: {path}"
    return None
