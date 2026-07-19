"""Subprocess plumbing for the restart pipeline: checks + git, argv-only.

Split from ``pipeline.py`` (which owns the guarded-restart orchestration) so
the seatbelt reads as policy and this file as mechanism. Everything runs with
explicit argument lists (never a shell), so rationales cannot inject commands.
"""

import asyncio
import subprocess
from collections.abc import Sequence
from pathlib import Path

from chief.selfedit.recovery import MARKER_NAME

# One hung check (network test, stdin prompt) must not hold the restart lock
# forever — the same wedge class as the fixed chief-pkg clone hang (0931964).
CHECK_TIMEOUT_SECONDS = 600.0


def _capture(
    argv: Sequence[str], cwd: Path, timeout: float | None = None
) -> tuple[int, str]:
    """Run ``argv`` (no shell) and return (returncode, combined output)."""
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, f"check timed out after {timeout:.0f}s"
    return proc.returncode, proc.stdout


async def run_check(cmd: Sequence[str], root: Path) -> tuple[int, str]:
    """One done-check command off the event loop, bounded by the timeout."""
    return await asyncio.to_thread(_capture, cmd, root, CHECK_TIMEOUT_SECONDS)


async def run_git(root: Path, *args: str) -> str:
    """Run a git command in ``root``; raise on a non-zero exit."""
    code, output = await asyncio.to_thread(_capture, ("git", *args), root)
    if code != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    return output


async def dirty_files(root: Path) -> list[str]:
    """Changed working-tree paths, excluding the pipeline's own marker.

    The rollback marker is the pipeline's runtime state, written post-commit
    and normally cleared on the next healthy boot. If it lingers (recovery
    skipped, crash), it must not count as a change and commit an empty
    marker-only edit. ``.gitignore`` also lists it so human ``git status``
    stays clean; this filter is belt-and-suspenders.
    """
    lines = (await run_git(root, "status", "--porcelain")).splitlines()
    paths = [line[3:].strip() for line in lines if line.strip()]
    return [path for path in paths if path != MARKER_NAME]
