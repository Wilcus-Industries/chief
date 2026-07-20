"""Keeping the chief-packages clone present and current.

Both operations are bounded and fail soft, and that is the whole point:
``chief-pkg`` runs through the single dispatcher, so a git command that hangs
here hangs the entire daemon — which is exactly what an unbounded clone did
once. A stale clone is always the better failure.
"""

import os
import subprocess
import sys
from pathlib import Path

#: Hard ceiling on the one-time clone so a stalled network or auth prompt can never
#: wedge `chief-pkg` (and, through the single dispatcher, the whole daemon).
CLONE_TIMEOUT_SECONDS = 30.0

#: Tighter than the clone: this one runs on EVERY invocation, so it is latency
#: on every `chief-pkg list` the agent makes. A slow network degrades to a
#: stale clone rather than a slow daemon.
PULL_TIMEOUT_SECONDS = 10.0


def clone_if_missing(repo_url: str, dest: Path) -> None:
    """Clone the chief-packages repo once; never fail OR hang the CLI if it can't.

    The remote may not exist yet, so a clone failure is a warning, not an error —
    discovery still works over the bundled root alone. ``GIT_TERMINAL_PROMPT=0`` stops
    git blocking forever on an interactive auth prompt, and a hard ``timeout`` bounds a
    stalled network; either way discovery falls back to bundled-only.
    """
    if dest.exists() or not repo_url:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"warning: clone of {repo_url} timed out after "
              f"{CLONE_TIMEOUT_SECONDS:g}s", file=sys.stderr)
        return
    if result.returncode != 0:
        print(f"warning: could not clone {repo_url}: {result.stderr.strip()}",
              file=sys.stderr)


def pull_clone(dest: Path) -> str:
    """Fast-forward the packages clone. Never raises, never hangs.

    Returns a one-line summary; only ``update`` prints it, so ordinary
    ``list``/``search`` output stays clean. Failures always warn on stderr
    regardless, because silently serving a stale clone is exactly how a package
    edit appears to have "not deployed".
    """
    if not (dest / ".git").is_dir():
        return "no clone to update"
    try:
        result = subprocess.run(
            ["git", "-C", str(dest), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=PULL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        message = f"pull timed out after {PULL_TIMEOUT_SECONDS:g}s — clone left as is"
        print(f"warning: {message}", file=sys.stderr)
        return message
    if result.returncode != 0:
        message = f"could not pull packages: {result.stderr.strip()}"
        print(f"warning: {message}", file=sys.stderr)
        return message
    summary = result.stdout.strip().splitlines()
    return summary[-1] if summary else "up to date"
