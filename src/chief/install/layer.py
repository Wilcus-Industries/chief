"""The local layer: a box's self-edits, three-way applied onto a release.

A box's local layer is the **tree difference** between its pinned base and its
working tree — deliberately not a commit range. An update commits as one
squashed change, so after the first update HEAD's parent is the previous box
commit, not the release it came from; a rebase or a commit range could no
longer tell the local layer from all of history. A tree diff against a pinned
base is immune to both that and to upstream rewriting its own history.

The application is a real three-way merge (``git merge-tree``) with the pin
supplied as an explicit merge base, so it never asks git for a common
ancestor. The merged tree is then written into the index and working tree with
HEAD left alone, which is what leaves the result as uncommitted changes on the
pre-update commit — exactly the shape the self-edit seatbelt already commits.

Collisions are left as conflict markers on purpose. Resolving them with a
strategy flag is the bug this replaces: ``-X theirs`` discarded self-edits
silently.
"""

import os
import subprocess
from pathlib import Path

#: Exit code for "applied, but some files collided and need resolving".
CONFLICTS_EXIT = 2

#: Never let a git call block on an interactive credential prompt — an
#: auth-required fetch must fail fast, not hang until the timeout. Mirrors the
#: hardening in :mod:`.updatecheck`.
_NO_PROMPT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

#: Local git work is fast; a fetch is not, so it carries its own bound.
GIT_TIMEOUT_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 120.0


class GitError(RuntimeError):
    """A git command failed in a way the update cannot continue past."""


def git(repo_dir: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """Run git in ``repo_dir``; raise :class:`GitError` on failure."""
    code, out, err = _run(repo_dir, args, timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args)} failed: {(err or out).strip()}")
    return out


def head(repo_dir: Path) -> str:
    return git(repo_dir, "rev-parse", "HEAD").strip()


def dirty_tracked(repo_dir: Path) -> str:
    """Modified tracked files, if any. Untracked instance data never counts."""
    return git(repo_dir, "status", "--porcelain", "--untracked-files=no").strip()


def fetch(repo_dir: Path) -> str | None:
    """Fetch branches and tags. Returns an error message, or ``None`` on success.

    ``--force`` because upstream may have moved a tag; ``--prune-tags`` would
    delete the box's own record of a release it still runs, so it is not used.
    """
    code, out, err = _run(
        repo_dir,
        ("fetch", "--force", "--tags", "origin"),
        FETCH_TIMEOUT_SECONDS,
    )
    return None if code == 0 else (err or out).strip()


def merge_layer(
    repo_dir: Path, *, base: str, ours: str, theirs: str
) -> tuple[str, list[str]]:
    """Three-way merge ``ours`` and ``theirs`` over the explicit base ``base``.

    Returns the merged tree's object id and the conflicted paths. ``ours`` and
    ``theirs`` double as the conflict-marker labels, so pass readable names
    (``HEAD`` and the release tag) rather than raw object ids.
    """
    code, out, err = _run(
        repo_dir,
        (
            "merge-tree",
            "--write-tree",
            "-z",
            f"--merge-base={base}",
            ours,
            theirs,
        ),
        GIT_TIMEOUT_SECONDS,
    )
    if code > 1:
        raise GitError(f"git merge-tree failed: {(err or out).strip()}")
    records = out.split("\0")
    tree = records[0].strip()
    if not tree:
        raise GitError("git merge-tree wrote no tree")
    return tree, _conflicted_paths(records[1:])


def checkout_tree(repo_dir: Path, tree: str) -> None:
    """Put ``tree`` into the index and working tree, leaving HEAD where it is.

    That is what stages the update as ordinary uncommitted changes on the
    pre-update commit. Paths no release tracks — installed skills, config,
    secrets, ``data/`` — are untouched, because they are in no tree at all. The
    one exception is a release that starts tracking a path an install already
    holds as an untracked file: ``--reset`` would overwrite it. That cannot hit
    the gitignored instance dirs (a release will not track into them), but a
    new top-level tracked file colliding with local scratch would be clobbered.
    """
    git(repo_dir, "read-tree", "-u", "--reset", tree)


def _conflicted_paths(records: list[str]) -> list[str]:
    """Paths from merge-tree's conflicted-file records (``mode obj stage\\tpath``).

    The records run until an empty one, after which merge-tree emits its
    informational messages; each conflicted path appears once per stage.
    """
    paths: list[str] = []
    for record in records:
        if not record:
            break
        _, _, path = record.partition("\t")
        if path and path not in paths:
            paths.append(path)
    return paths


def _run(
    repo_dir: Path, args: tuple[str, ...], timeout: float
) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). Kept apart: merge-tree's stdout is parsed
    record-by-record, so a warning on stderr must never land inside it."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_NO_PROMPT_ENV,
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"git {args[0]} timed out after {timeout:.0f}s"
    return result.returncode, result.stdout, result.stderr
