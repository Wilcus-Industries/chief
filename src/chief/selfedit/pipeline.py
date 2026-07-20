"""The guarded restart pipeline — the seatbelt for self-editing.

The agent edits its working tree freely with the file tools; nothing is live
until ``restart``: dirty-tree file-list confirmation, the full done-check,
then a live ``config.yaml`` load (fixture configs alone would let a bad real
value boot-loop launchd). Only on all green does it commit, write the
rollback marker, and reboot into the new code (a boot failure rolls back —
``recovery.py``). On red the edits are **kept** and the failure returned so
the agent fixes forward. Restarts serialize behind a lock.

Subprocess plumbing (checks, git, argv-only — no shell) lives in
``gitops.py``.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

from chief.agent.tools import ToolContext
from chief.audit import AuditLog
from chief.config import load_config
from chief.selfedit.gitops import dirty_files, run_checks, run_git
from chief.selfedit.notice import record_restart_origin
from chief.selfedit.recovery import MARKER_NAME

logger = logging.getLogger(__name__)

# The project done-check (CLAUDE.md); injectable so tests use a fast stand-in.
DEFAULT_CHECKS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "mypy", "."),
)


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
        # Loads the live config.yaml pre-restart so a bad real value aborts
        # here instead of boot-looping launchd (injectable for tests).
        self._validate_config = validate_config
        # Serialize check/commit/re-exec so concurrent sessions can't interleave
        # a restart (PRD #198). The deferred re-exec still races other sessions'
        # writes between restarts — an accepted, documented tradeoff.
        self._lock = asyncio.Lock()
        # Circuit breaker: consecutive red done-checks. The agent has looped
        # edit -> red -> edit before (once editing a recursion into config.py);
        # each lap burns a full pytest+ruff+mypy run while the owner is blind.
        self._consecutive_failures = 0

    async def restart(
        self, rationale: str, confirm: bool = False, origin: ToolContext | None = None
    ) -> str:
        """Run the done-check against the working tree, then restart on green.

        A dirty tree first requires confirmation: the changed-file list comes
        back (before any expensive check) and the agent must call again with
        ``confirm=True`` — so working-tree debris is never swept into a commit
        whose rationale describes a different change.

        Green: commit (if dirty), write the rollback marker, request the
        restart. Red: keep the edits and return the failure. A no-op restart
        is allowed, covering config reloads and script-only installs.
        ``origin`` is the calling thread, recorded so the rebooted daemon
        reports back there instead of leaving the owner to poke the thread.
        """
        async with self._lock:
            return await self._guarded_restart(rationale, confirm, origin)

    async def _guarded_restart(
        self, rationale: str, confirm: bool, origin: ToolContext | None = None
    ) -> str:
        dirty = await self._dirty_files()
        if dirty and not confirm:
            file_list = "\n".join(f"  {path}" for path in dirty)
            self._audit.record(
                "restart", outcome="confirm_needed", rationale=rationale, files=dirty
            )
            return (
                "confirm: this restart will commit the files below under the "
                f"rationale {rationale!r}:\n{file_list}\n"
                "If every file belongs to this change, call restart again with "
                "confirm=true. If something does not belong, first discard it "
                "(shell: git checkout HEAD -- <path>) or restart separately "
                "with a "
                "rationale that honestly describes it."
            )
        base = (await self._git("rev-parse", "HEAD")).strip()
        failure = await run_checks(self._checks, self._root)
        if failure is not None:
            self._consecutive_failures += 1
            self._audit.record(
                "restart", outcome="check_failed", streak=self._consecutive_failures
            )
            message = f"error: done-check failed; your edits are kept.\n{failure}"
            if self._consecutive_failures >= 3:
                message += (
                    f"\n\nThis is {self._consecutive_failures} failed "
                    "done-checks in a row. STOP editing forward. Show the "
                    "owner the diff (shell: git diff) and ask how to proceed, "
                    "or discard the broken edits with the revert_edits tool."
                )
            return message
        self._consecutive_failures = 0
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
            "restart",
            outcome="restarting",
            rationale=rationale,
            committed=committed,
            files=dirty,
        )
        logger.info("restart approved (%s); committed=%s", rationale, committed)
        record_restart_origin(self._root, origin, rationale)
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
        if not await self._dirty_files():
            return False
        await self._git("add", "-A")
        await self._git("commit", "-m", f"self-edit: {rationale}")
        marker = {"rollback_to": base, "rationale": rationale}
        (self._root / MARKER_NAME).write_text(json.dumps(marker))
        return True

    async def revert_edits(self) -> str:
        """Discard uncommitted changes to tracked repo files (back to HEAD).

        The safe exit from an edit -> red-check loop. Untracked files are left
        alone (they may be data the agent still wants) but are reported so
        nothing is silently forgotten."""
        async with self._lock:
            dirty = await self._dirty_files()
            if not dirty:
                return "nothing to revert: the working tree is clean"
            # HEAD explicitly: bare `checkout -- .` restores from the INDEX,
            # so content staged via a shell `git add` would survive (#234).
            await self._git("checkout", "HEAD", "--", ".")
            remaining = await self._dirty_files()
            reverted = [path for path in dirty if path not in remaining]
            self._audit.record("revert_edits", outcome="reverted", files=reverted)
            self._consecutive_failures = 0
            lines = "\n".join(f"  {path}" for path in reverted) or "  (none)"
            message = f"reverted tracked files to HEAD:\n{lines}"
            if remaining:
                left = "\n".join(f"  {path}" for path in remaining)
                message += f"\nuntracked files left in place:\n{left}"
            return message

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

    async def _dirty_files(self) -> list[str]:
        return await dirty_files(self._root)

    async def _git(self, *args: str) -> str:
        return await run_git(self._root, *args)

