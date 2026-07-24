"""The base pin: which release this box last synced to.

The pin is a **local** git ref. That is the whole point — a remote-tracking
branch or an upstream tag disappears the moment upstream rewrites its history
(which has happened once already and left a box unupdatable). A local ref also
keeps the commit object itself alive against gc, so the three-way base is
still there to merge against long after upstream has forgotten it.

The pin advances **only after an update comes back healthy**. ``update`` writes
a pending record naming the release and the pre-update HEAD; the next healthy
boot advances the pin if — and only if — a commit actually landed. A rollback,
a red done-check, or an abandoned update all leave HEAD where it was, so the
same check discards the record without moving the pin.
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Under ``refs/chief/`` so nothing git does to branches or tags touches it.
BASE_REF = "refs/chief/base"

#: Runtime state, gitignored with the rest of ``data/``.
PENDING_NAME = "data/update_pending.json"


@dataclass(frozen=True)
class Pending:
    """An applied-but-not-yet-proven update."""

    commit: str
    version: str
    head: str


def read_base(repo_dir: Path) -> str | None:
    """The pinned release commit, or ``None`` when the box has never synced."""
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", BASE_REF)


def write_base(repo_dir: Path, commit: str) -> bool:
    """Pin ``commit`` as the base the local layer is measured against.

    Resolves it first: ``git update-ref`` reads an all-zero or unknown object
    as *delete the ref*, so an unusable value would quietly leave the box with
    no pin at all — the one state an update cannot recover from on its own.
    """
    resolved = _git(
        repo_dir, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"
    )
    if resolved is None:
        logger.error("refusing to pin an unknown commit: %s", commit)
        return False
    _git(repo_dir, "update-ref", BASE_REF, resolved)
    return True


def record_pending(
    repo_dir: Path, *, commit: str, version: str, head: str
) -> None:
    """Note the release just applied and the HEAD it was applied on top of."""
    path = repo_dir / PENDING_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"commit": commit, "version": version, "head": head})
    )


def read_pending(repo_dir: Path) -> Pending | None:
    """The pending record, or ``None`` when there is none (or it is corrupt)."""
    try:
        raw = json.loads((repo_dir / PENDING_NAME).read_text())
        return Pending(
            commit=str(raw["commit"]),
            version=str(raw["version"]),
            head=str(raw["head"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resolve_pending(repo_dir: Path) -> str | None:
    """Settle a pending update on a healthy boot; return the version pinned.

    Advances the pin only when HEAD has moved off the pre-update commit —
    proof that the seatbelt's done-check went green and committed. Either way
    the record is cleared, so a stale one can never advance the pin later.
    """
    pending = read_pending(repo_dir)
    if pending is None:
        return None
    (repo_dir / PENDING_NAME).unlink(missing_ok=True)
    head = _git(repo_dir, "rev-parse", "HEAD")
    if head is None or head == pending.head:
        logger.info("update to %s did not land; base pin unmoved", pending.version)
        return None
    if not write_base(repo_dir, pending.commit):
        return None
    logger.info("update to %s came back healthy; base pinned", pending.version)
    return pending.version


def _git(repo_dir: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
