"""``chief update``: apply the newest release onto this box's self-edits.

Deterministic git only. It fetches, works out the newest release, three-way
applies the box's local layer onto it (:mod:`.layer`), and leaves the result
as uncommitted working-tree changes on the pre-update commit. It commits
nothing, restarts nothing, and resolves no conflicts — chief does that, by
reading this command's output and then calling the ``restart`` tool, whose
done-check / single-commit / boot-rollback seatbelt this deliberately reuses
verbatim.

It never checks a release out. A running chief edits its own source, so every
install carries local commits no release contains; a checkout would drop that
layer on the floor. The layer is applied *onto* the release, which is also why
a collision keeps the self-edit's side instead of discarding it.

Exit codes: ``0`` applied cleanly (or nothing to do), :data:`layer.CONFLICTS_EXIT`
applied with conflicts to resolve, ``1`` could not run.
"""

from collections.abc import Callable
from pathlib import Path

from chief.install import basepin, layer, releases

NO_PIN_MESSAGE = (
    "no base pin recorded, so there is nothing to measure this box's local "
    "layer against. The migration records one at boot; if this box has never "
    "run it, pin the release it is on by hand:\n"
    f"  git update-ref {basepin.BASE_REF} v<X.Y.Z>"
)


def update(*, repo_dir: Path, say: Callable[[str], None] = print) -> int:
    """Apply the newest release onto the working tree. See the module doc."""
    try:
        return _update(repo_dir=repo_dir, say=say)
    except layer.GitError as exc:
        say(f"error: {exc}")
        return 1


def abort_update(*, repo_dir: Path, say: Callable[[str], None] = print) -> int:
    """Give up on an applied-but-unresolved update, cleanly.

    Undoing the apply is not enough on its own: the pending record must be
    cleared in the same breath. If only the tree were reverted (what
    ``revert_edits`` does), the record would linger with HEAD still on the
    pre-update commit, and the next unrelated self-edit's healthy boot would
    read that HEAD movement as the update landing — pinning a release whose
    changes are no longer in the tree. So the box's give-up path calls this,
    not the generic revert.

    A no-op when nothing is pending, so it can never discard real work: only an
    update in flight is undone, and only back onto the commit it was applied on.
    """
    try:
        if basepin.read_pending(repo_dir) is None:
            say("nothing to abort: no update is in flight.")
            return 0
        # Inverse of the apply: reset index + working tree to HEAD (the
        # pre-update commit), which drops the merged tree — added files and
        # all — while leaving untracked instance data untouched.
        layer.checkout_tree(repo_dir, "HEAD")
        basepin.clear_pending(repo_dir)
        say("aborted: the update was undone; still on the pinned release.")
        return 0
    except layer.GitError as exc:
        say(f"error: {exc}")
        return 1


def _update(*, repo_dir: Path, say: Callable[[str], None]) -> int:
    failure = layer.fetch(repo_dir)
    if failure is not None:
        say(f"git fetch failed: {failure}")
        return 1
    release = releases.newest_release(repo_dir)
    if release is None:
        say("no releases found upstream — nothing to update to.")
        return 1
    base = basepin.read_base(repo_dir)
    if base is None:
        say(NO_PIN_MESSAGE)
        return 1
    if base == release.commit:
        say(f"already up to date ({release.tag}).")
        return 0
    dirty = layer.dirty_tracked(repo_dir)
    if dirty:
        say(
            "uncommitted changes to tracked files — commit or discard them "
            f"first, or they would be discarded by the update:\n{dirty}"
        )
        return 1
    return _apply(repo_dir=repo_dir, release=release, base=base, say=say)


def _apply(
    *,
    repo_dir: Path,
    release: releases.Release,
    base: str,
    say: Callable[[str], None],
) -> int:
    before = layer.head(repo_dir)
    tree, conflicts = layer.merge_layer(
        repo_dir, base=base, ours="HEAD", theirs=release.tag
    )
    layer.checkout_tree(repo_dir, tree)
    # Written before the outcome is reported so the record exists however the
    # caller reacts. It only advances the pin once a commit has landed AND the
    # box has come back healthy, so recording it here cannot pin a failure.
    basepin.record_pending(
        repo_dir, commit=release.commit, version=release.tag, head=before
    )
    if not conflicts:
        say(f"clean: {release.tag} applied onto this box's local layer.")
        say(_NEXT_STEP)
        return 0
    listed = "\n".join(f"  {path}" for path in conflicts)
    say(
        f"conflicts: {release.tag} applied, but these files changed on both "
        f"sides and carry conflict markers:\n{listed}"
    )
    say(
        "Resolve each one — keep the local edit's intent, take the release's "
        "structure — then run the done-check."
    )
    say(_NEXT_STEP)
    return layer.CONFLICTS_EXIT


_NEXT_STEP = (
    "The changes are staged on the pre-update commit and nothing is committed "
    "yet. Restart to commit them under the done-check (chief: the restart "
    "tool; by hand: `uv run pytest && uv run ruff check . && uv run mypy .` "
    "then commit)."
)
