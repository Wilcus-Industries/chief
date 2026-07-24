"""The one-time crossover onto the release-based update system.

Idempotent and run at boot, because that is the only place new code runs on a
box that updated with the *old* code. Three things, each a no-op once done:

* **Installed skills stop being tracked.** They are install-local copies chief
  edits about itself — the last piece of instance state still inside the git
  lineage, and the largest source of update collisions. They join
  ``config.yaml`` / ``secrets/`` / ``data/`` as untracked instance data.
* **Core's own skills are seeded** from the tracked ``core-skills/`` originals,
  so a fresh clone (and a box whose tracked copies the crossover removed) still
  has them. An existing installed copy is never overwritten: it may be chief's
  own edit.
* **The base pin is recorded** at the release the box is actually on, so the
  first new-style update has something to measure its local layer against.
"""

import logging
import shutil
from pathlib import Path

from chief.install import basepin, layer, releases

logger = logging.getLogger(__name__)

CORE_SKILLS_DIR = "core-skills"
SKILLS_DIR = "skills"
IGNORE_ENTRY = "/skills/"
_IGNORE_COMMENT = (
    "# Installed skill copies — instance data chief edits about itself.\n"
)


def migrate_instance(repo_dir: Path) -> list[str]:
    """Run every pending crossover step; return what was done (``[]`` = none).

    Fail-soft by contract: this runs on the boot path, so a git problem here
    is logged and skipped rather than allowed to take the daemon down.
    """
    done: list[str] = []
    try:
        if layer.dirty_tracked(repo_dir):
            logger.info("skipping the update migration: the tree is dirty")
            return done
        if _untrack_skills(repo_dir):
            done.append("untracked installed skills")
        if _seed_core_skills(repo_dir):
            done.append("seeded core skills")
        if _record_base(repo_dir):
            done.append("recorded the base pin")
    except layer.GitError:
        logger.exception("update migration skipped")
    return done


def _untrack_skills(repo_dir: Path) -> bool:
    """Drop ``skills/`` from the index and ignore it, in one commit.

    The working-tree copies are kept (``--cached``). Committing immediately
    matters: a staged deletion left lying around would otherwise be swept into
    whatever the next self-edit commits, under someone else's rationale.
    """
    tracked = bool(layer.git(repo_dir, "ls-files", SKILLS_DIR).strip())
    ignored = _ensure_ignored(repo_dir)
    if not tracked and not ignored:
        return False
    if tracked:
        layer.git(repo_dir, "rm", "-r", "--cached", "-q", SKILLS_DIR)
    if ignored:
        layer.git(repo_dir, "add", ".gitignore")
    layer.git(
        repo_dir,
        "commit",
        "-q",
        "-m",
        "chore: untrack installed skills (update migration)",
    )
    logger.info("installed skills are now untracked instance data")
    return True


def _ensure_ignored(repo_dir: Path) -> bool:
    """Add ``/skills/`` to .gitignore if absent; True when the file changed."""
    path = repo_dir / ".gitignore"
    text = path.read_text() if path.exists() else ""
    if IGNORE_ENTRY in text.splitlines():
        return False
    prefix = text if text.endswith("\n") or not text else text + "\n"
    path.write_text(f"{prefix}\n{_IGNORE_COMMENT}{IGNORE_ENTRY}\n")
    return True


def _seed_core_skills(repo_dir: Path) -> bool:
    """Copy tracked core skills into ``skills/``, never over an existing file.

    File-by-file rather than directory-by-directory: a skill directory that
    exists but has lost its SKILL.md still needs seeding, and an installed
    file that is present may be chief's own edit and is left alone.
    """
    source = repo_dir / CORE_SKILLS_DIR
    if not source.is_dir():
        return False
    seeded = False
    for original in sorted(source.rglob("*")):
        # Only what lives inside a skill directory; the source dir's own README
        # documents the tracked side and has no business in an install.
        if not original.is_file() or original.parent == source:
            continue
        target = repo_dir / SKILLS_DIR / original.relative_to(source)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        logger.info("seeded core skill file %s", target.name)
        seeded = True
    return seeded


def _record_base(repo_dir: Path) -> bool:
    """Pin the release this box is on, if it has no pin yet.

    Prefers the newest release contained in HEAD — that is literally the
    release the box runs. Failing that (a box whose lineage upstream has since
    rewritten) the merge base against the newest release is the honest answer.
    Neither available means no safe base exists, and inventing one would apply
    the next update as a wholesale takeover, so the pin is left unset.
    """
    if basepin.read_base(repo_dir) is not None:
        return False
    known = releases.all_releases(repo_dir)
    if not known:
        return False
    for release in reversed(known):
        if _contains(repo_dir, release.commit):
            basepin.write_base(repo_dir, release.commit)
            logger.info("base pinned at %s", release.tag)
            return True
    shared = _merge_base(repo_dir, known[-1].commit)
    if shared is None:
        logger.error(
            "no base pin could be recorded: HEAD shares no history with %s. "
            "Pin the release this box is on by hand: git update-ref %s v<X.Y.Z>",
            known[-1].tag,
            basepin.BASE_REF,
        )
        return False
    basepin.write_base(repo_dir, shared)
    logger.info("base pinned at the shared ancestor of %s", known[-1].tag)
    return True


def _contains(repo_dir: Path, commit: str) -> bool:
    try:
        layer.git(repo_dir, "merge-base", "--is-ancestor", commit, "HEAD")
    except layer.GitError:
        return False
    return True


def _merge_base(repo_dir: Path, commit: str) -> str | None:
    try:
        return layer.git(repo_dir, "merge-base", "HEAD", commit).strip() or None
    except layer.GitError:
        return None
