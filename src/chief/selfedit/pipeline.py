"""The guarded restart pipeline — the seatbelt for self-editing.

The agent edits its working tree freely with the file tools; nothing is live
until ``restart``. ``restart`` runs the full done-check against the working
tree, then loads the live ``config.yaml`` (the done-check only exercises
fixture configs, so a bad real value would otherwise pass every check and
boot-loop launchd — boot-side rollback can't rescue a config-only change), and
only on both green commits the tree, writes a rollback marker, and reboots the
daemon into the new code (a boot failure rolls back to the recorded commit,
see ``recovery.py``). On red — check or config — the edits are **kept in
place** and the failure is returned so the agent fixes forward. Restarts are
serialized behind a lock.

Subprocesses run with explicit argument lists (never a shell), so rationales
cannot inject commands.
"""

import asyncio
import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from chief.audit import AuditLog
from chief.config import load_config
from chief.selfedit.recovery import MARKER_NAME

logger = logging.getLogger(__name__)

# The project done-check (CLAUDE.md); injectable so tests use a fast stand-in.
DEFAULT_CHECKS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "mypy", "."),
)


def _capture(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run ``argv`` (no shell) and return (returncode, combined output)."""
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode, proc.stdout


class SelfEditPipeline:
    """Runs the done-check and, on green, commits + restarts into the tree."""

    def __init__(
        self,
        repo_root: Path,
        audit: AuditLog,
        restart: Callable[[], None],
        checks: tuple[tuple[str, ...], ...] = DEFAULT_CHECKS,
        validate_config: Callable[[], object] = load_config,
    ) -> None:
        self._root = repo_root
        self._audit = audit
        self._restart = restart
        self._checks = checks
        # Load the live config.yaml before restart so a bad real value aborts
        # here instead of boot-looping launchd. The done-check only exercises
        # fixture configs; load_config is side-effect-free (injectable for tests).
        self._validate_config = validate_config
        # Serialize check/commit/re-exec so concurrent sessions can't interleave
        # a restart (PRD #198). The deferred re-exec still races other sessions'
        # writes between restarts — an accepted, documented tradeoff.
        self._lock = asyncio.Lock()

    async def restart(self, rationale: str) -> str:
        """Run the done-check against the working tree, then restart on green.

        Green: commit the tree (if it changed), write the rollback marker, and
        request a restart into the new code. Red: keep the edits in place and
        return the failure output. A no-op (no repo changes) is allowed — it
        still restarts, covering config reloads and script-only installs.
        """
        async with self._lock:
            return await self._guarded_restart(rationale)

    async def _guarded_restart(self, rationale: str) -> str:
        base = (await self._git("rev-parse", "HEAD")).strip()
        failure = await self._run_checks()
        if failure is not None:
            self._audit.record("restart", outcome="check_failed")
            return f"error: done-check failed; your edits are kept.\n{failure}"
        config_error = await self._validate_live_config()
        if config_error is not None:
            self._audit.record("restart", outcome="config_invalid")
            return (
                "error: config.yaml is invalid; your edits are kept and the "
                "daemon was NOT restarted (a bad config would boot-loop the "
                f"reboot).\n{config_error}"
            )
        committed = await self._commit_if_dirty(rationale, base)
        self._audit.record(
            "restart", outcome="restarting", rationale=rationale, committed=committed
        )
        logger.info("restart approved (%s); committed=%s", rationale, committed)
        self._restart()
        if committed:
            return "done-check green; committed and restarting into the new code"
        return "done-check green; no repo changes, restarting"

    async def _commit_if_dirty(self, rationale: str, base: str) -> bool:
        """Commit tracked working-tree changes and drop a rollback marker.

        Returns whether anything was committed. Gitignored writes (``data``,
        ``secrets``, ``config.yaml``, off-repo) never enter the commit, so they
        are unversioned — the rollback marker only rewinds repo files (#198).
        """
        if not await self._repo_dirty():
            return False
        await self._git("add", "-A")
        await self._git("commit", "-m", f"self-edit: {rationale}")
        marker = {"rollback_to": base, "rationale": rationale}
        (self._root / MARKER_NAME).write_text(json.dumps(marker))
        return True

    async def _validate_live_config(self) -> str | None:
        """Load the live config off the event loop; return the error on failure.

        A config-only change writes no rollback marker (marker is commit-only)
        and git can't rewind gitignored ``config.yaml`` anyway, so boot-side
        recovery can't save a bad-config reboot — this pre-restart gate is the
        only prevention. Any load exception aborts the restart."""
        try:
            await asyncio.to_thread(self._validate_config)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    async def _run_checks(self) -> str | None:
        """Run every check command; return combined output on first failure."""
        for cmd in self._checks:
            code, output = await asyncio.to_thread(_capture, cmd, self._root)
            if code != 0:
                return f"$ {' '.join(cmd)}\n{output[-4000:]}"
        return None

    async def _repo_dirty(self) -> bool:
        # The rollback marker is the pipeline's own runtime state, written
        # post-commit and normally cleared on the next healthy boot. If it
        # lingers (recovery skipped, crash), it must not count as a change and
        # commit an empty marker-only edit. `.gitignore` also lists it so human
        # `git status` stays clean; this filter is belt-and-suspenders.
        lines = (await self._git("status", "--porcelain")).splitlines()
        return any(line[3:].strip() != MARKER_NAME for line in lines if line.strip())

    async def _git(self, *args: str) -> str:
        code, output = await asyncio.to_thread(_capture, ("git", *args), self._root)
        if code != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {output}")
        return output
