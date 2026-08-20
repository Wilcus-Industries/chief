"""Boot-side half of the self-edit seatbelt: undoing a restart that failed.

The pipeline leaves a marker file before restarting. If the new code boots,
the marker is cleared (healthcheck passed). If boot raises, whatever the
restart changed is undone and the daemon restarts into the old state — the
repo resets to the recorded commit, and the config is put back from its
newest history snapshot, which git cannot do because config.yaml is ignored.

The other half — deciding when the restart is allowed to fire at all — lives
in ``chief.selfedit.restart``.
"""

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from chief.config.history import restore_newest

logger = logging.getLogger(__name__)

MARKER_NAME = ".selfedit-pending.json"

#: The config a failed boot was rolled back *from*, kept beside the repo so a
#: hand edit is never silently discarded by the recovery.
FAILED_CONFIG_NAME = ".config-failed.yaml"


def clear_marker(repo_root: Path) -> None:
    """Boot succeeded: the self-edit (if any) is healthy."""
    marker = repo_root / MARKER_NAME
    if marker.exists():
        logger.info("self-edit healthcheck passed: %s", marker.read_text())
        marker.unlink()


def rollback_if_marked(
    repo_root: Path,
    config_history: Path | None = None,
    run: Callable[[list[str]], object] | None = None,
) -> str:
    """After a failed boot: undo whatever the restart changed.

    Two halves, because a restart changes two things the seatbelt covers
    differently. Committed repo files rewind with ``git reset --hard``. The
    config does not — it is gitignored, so git steps straight past it, and a
    config-only restart commits nothing at all, which is why it used to leave
    no marker and so had no boot-failure recovery for anything. Its undo is
    the newest config-history snapshot.

    Returns what was undone, empty when nothing was — the caller reads that
    as "do not reboot", since an identical tree and config reproduce the same
    failed boot forever. The text also reaches the owner's restart notice, so
    the report names the half that moved instead of assuming both did.

    Neither half may raise: the marker is consumed before either runs, so an
    escape would spend the seatbelt and undo nothing — and a git failure would
    skip the config half, which may be the undo this boot needs.

    ``config_history`` comes from the caller rather than the marker: the boot
    that writes the marker and the boot that reads it resolve the data dir the
    same way, so recording it would only let the two drift.

    ``run`` is the git runner, injected by tests so they need no real repo.
    """
    marker = repo_root / MARKER_NAME
    if not marker.exists():
        return ""
    record = json.loads(marker.read_text())
    marker.unlink()
    undone = []
    # ``committed`` is absent from a marker written by the previous version,
    # and those were only ever written when a commit had been made.
    if record.get("committed", True) and record.get("rollback_to"):
        target = str(record["rollback_to"])
        logger.error("boot failed after self-edit; rolling back to %s", target)
        argv = ["git", "reset", "--hard", target]
        try:
            if run is None:
                subprocess.run(argv, cwd=repo_root, check=True)
            else:
                run(argv)
            undone.append(f"the code is back on {target}")
        except (subprocess.CalledProcessError, OSError):
            logger.exception("could not roll the repo back to %s", target)
    if config_history is not None:
        try:
            restored = restore_newest(
                config_history,
                repo_root / "config.yaml",
                repo_root / FAILED_CONFIG_NAME,
            )
        except OSError:
            # Same reasoning as the snapshot side: a full disk must not turn a
            # recoverable boot failure into an unrecoverable one.
            logger.exception("could not put the config back from %s", config_history)
            restored = None
        if restored is not None:
            logger.error("boot failed; put the config back from %s", restored)
            undone.append(
                f"config.yaml is back from {restored.name}, and the one that "
                f"failed is kept at {FAILED_CONFIG_NAME}"
            )
    if not undone:
        # Naming the history dir here is the one signal that a relocated
        # ``db_path`` left the config half looking somewhere empty.
        logger.error(
            "boot failed, but nothing was left to undo (config history: %s); "
            "not restarting",
            config_history,
        )
    return "; ".join(undone)


