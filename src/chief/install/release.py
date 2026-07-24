"""``chief release [major|minor|patch]``: cut a release boxes can update onto.

Upstream-side only — nothing here runs on a box. A release is a ``vX.Y.Z``
tag plus a GitHub Release, and it cannot be cut from a dirty tree or a red
done-check: every box in the fleet applies its own self-edits onto whatever
this publishes, so shipping a broken tree costs a rollback everywhere at once.
"""

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from chief.install import releases
from chief.selfedit.checks import DEFAULT_CHECKS

#: Publishes the GitHub Release: ``(tag, notes) -> None``. Injectable so the
#: suite can cut real tags in a real repo without a network or a gh login.
Publish = Callable[[str, str], None]

#: A full done-check run is minutes, not seconds.
CHECK_TIMEOUT_SECONDS = 1800.0

#: A push that needs auth must fail fast, not hang on a credential prompt.
_NO_PROMPT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def cut_release(
    *,
    repo_dir: Path,
    part: str,
    checks: Sequence[Sequence[str]] = DEFAULT_CHECKS,
    publish: Publish | None = None,
    say: Callable[[str], None] = print,
) -> int:
    """Bump, tag, push, and publish. Returns the process exit code."""
    try:
        current = releases.read_project_version(repo_dir / "pyproject.toml")
        next_version = current.bump(part)
    except ValueError as exc:
        say(f"error: {exc}")
        return 1
    if _dirty(repo_dir):
        say(
            "uncommitted changes — a release must be cut from a clean tree:\n"
            f"{_dirty(repo_dir)}"
        )
        return 1
    if _tag_exists(repo_dir, str(next_version)):
        say(f"error: {next_version} already exists.")
        return 1
    say(f"running the done-check before tagging {next_version}…")
    failed = _first_failing(checks, repo_dir)
    if failed is not None:
        say(f"done-check failed; nothing tagged:\n{failed}")
        return 1
    notes = _notes(repo_dir, since=releases.newest_release(repo_dir))
    return _publish(
        repo_dir=repo_dir,
        version=next_version,
        notes=notes,
        publish=publish or _gh_release,
        say=say,
    )


def _publish(
    *,
    repo_dir: Path,
    version: releases.Version,
    notes: str,
    publish: Publish,
    say: Callable[[str], None],
) -> int:
    tag = str(version)
    releases.write_project_version(repo_dir / "pyproject.toml", version)
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(repo_dir, "commit", "-q", "-m", f"chore(release): {tag}", "pyproject.toml")
    _git(repo_dir, "tag", "-a", tag, "-m", tag)
    _git(repo_dir, "push", "-q", "origin", branch)
    _git(repo_dir, "push", "-q", "origin", tag)
    try:
        publish(tag, notes)
    except RuntimeError as exc:
        # The tag is the release as far as a box is concerned; the GitHub
        # Release is the human-facing half. Losing it is worth saying, not
        # worth unwinding a pushed tag over.
        say(f"warning: tag pushed but the GitHub Release failed: {exc}")
    say(f"released {tag} on {branch}.")
    return 0


def _notes(repo_dir: Path, *, since: releases.Release | None) -> str:
    """Subject lines since the previous release — what the release command
    can generate on its own, no more."""
    span = f"{since.tag}..HEAD" if since is not None else "HEAD"
    log = _git(repo_dir, "log", "--no-merges", "--format=- %s", span)
    return log.strip() or "No changes recorded."


def _gh_release(tag: str, notes: str) -> None:
    result = subprocess.run(
        ["gh", "release", "create", tag, "--title", tag, "--notes", notes],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh release create failed")


def _first_failing(
    checks: Sequence[Sequence[str]], repo_dir: Path
) -> str | None:
    for cmd in checks:
        try:
            result = subprocess.run(
                list(cmd),
                cwd=repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=CHECK_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"$ {' '.join(cmd)}\ntimed out after {CHECK_TIMEOUT_SECONDS:.0f}s"
        if result.returncode != 0:
            return f"$ {' '.join(cmd)}\n{result.stdout[-4000:]}"
    return None


def _dirty(repo_dir: Path) -> str:
    return _git(repo_dir, "status", "--porcelain", "--untracked-files=no").strip()


def _tag_exists(repo_dir: Path, tag: str) -> bool:
    return bool(_git(repo_dir, "tag", "--list", tag).strip())


def _git(repo_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=120.0,
            env=_NO_PROMPT_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout
