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
from collections.abc import Callable
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
        if await self._dirty():
            return "error: working tree is dirty; refusing to self-edit"
        base = (await self._git("rev-parse", "HEAD")).strip()
        branch = f"selfedit-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        await self._git("checkout", "-b", branch)
        try:
            for path, content in files.items():
                target = self._root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            await self._git("add", "-A")
            await self._git("commit", "-m", f"self-edit: {rationale}")
            failure = await self._run_checks()
        except Exception:
            await self._abandon(branch, base)
            raise
        if failure is not None:
            await self._abandon(branch, base)
            self._audit.record("self_edit", outcome="check_failed", files=list(files))
            return f"error: done-check failed, edit reverted\n{failure}"
        await self._git("checkout", "-")
        await self._git("merge", "--ff-only", branch)
        await self._git("branch", "-d", branch)
        marker = {"rollback_to": base, "rationale": rationale}
        (self._root / MARKER_NAME).write_text(json.dumps(marker))
        self._audit.record(
            "self_edit", outcome="merged", files=list(files), rationale=rationale
        )
        logger.info("self-edit merged (%s); restarting", rationale)
        self._restart()
        return "self-edit applied; restarting into the new code"

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

    async def _dirty(self) -> bool:
        return bool((await self._git("status", "--porcelain")).strip())

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
