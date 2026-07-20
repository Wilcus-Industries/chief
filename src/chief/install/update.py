"""``chief update``: merge origin/main, re-sync skills, restart, verify.

It **merges** — it never checks anything out. A running chief edits its own
source (the self-edit pipeline commits to this very repo), so every live
install carries local commits and diverges from any tag permanently. A tag
checkout would drop that local layer out of the working tree and silently roll
the box back to the release; a merge keeps it. There are no migrations to run
(the schema is created at boot by the new tree's code).

Installed ``skills/<name>/SKILL.md`` are install-time COPIES of the packaged
originals, so they are re-synced here — otherwise a package skill edit ships as
code while the agent goes on reading the stale copy.

This moves core and the BUNDLED packages, which share core's git repo, and
restarts the daemon. Cloned packages are ``chief-pkg update``, which restarts
nothing. The CLI wrapping lives in :mod:`.commands`.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

from chief.install.lifecycle import wait_for_health, web_url
from chief.install.service import ServiceManager
from chief.install.units import Runner


def sync_installed_skills(
    repo_dir: Path, *, runner: Runner, before: str
) -> tuple[list[str], list[str]]:
    """Advance installed SKILL.md copies, never clobbering a self-edit.

    Installed ``skills/`` files are copies of the packaged originals, and they
    go stale on their own. But chief self-edits its own installed skills, so a
    blind overwrite would destroy its work. An installed copy is safe to
    advance only when it still matches the packaged file as of ``before`` (the
    pre-merge commit) — that proves nobody has touched it since the last sync.
    Anything else has drifted and is left alone for a human to reconcile.

    Only skills already present in ``skills/`` are touched: installing a
    package is a separate, agent-driven step, and an update must never enable
    a capability the owner did not ask for.

    Returns ``(synced, drifted)`` skill names.
    """
    synced: list[str] = []
    drifted: list[str] = []
    for source in sorted(repo_dir.glob("packages/*/skills/*/SKILL.md")):
        name = source.parent.name
        target = repo_dir / "skills" / name / "SKILL.md"
        if not target.parent.is_dir():
            continue
        if target.is_file() and target.read_bytes() == source.read_bytes():
            continue  # already current
        if target.is_file():
            rel = source.relative_to(repo_dir).as_posix()
            was = runner(["git", "-C", str(repo_dir), "show", f"{before}:{rel}"])
            if was.returncode != 0 or was.stdout != target.read_text():
                drifted.append(name)
                continue
        shutil.copyfile(source, target)
        synced.append(name)
    return synced, drifted


def update(
    *,
    repo_dir: Path,
    runner: Runner,
    service: ServiceManager,
    say: Callable[[str], None] = print,
    healthy: Callable[[], bool] | None = None,
) -> int:
    """Merge origin/main: fetch, merge, sync, re-copy skills, restart, verify.

    Rolls the tree back to the pre-merge commit and restarts if the daemon
    does not answer afterwards, so a bad update cannot leave chief down.
    """
    git = ["git", "-C", str(repo_dir)]
    fetch = runner([*git, "fetch", "--force", "origin"])
    if fetch.returncode != 0:
        say(f"git fetch failed: {fetch.stderr.strip()}")
        return 1
    before = runner([*git, "rev-parse", "HEAD"]).stdout.strip()
    target = runner([*git, "rev-parse", "origin/main"]).stdout.strip()
    # NOT `before == target`: a self-editing install commits to this repo, so
    # HEAD is permanently ahead of origin/main and equality never holds. What
    # matters is whether origin/main is already contained in HEAD.
    contained = runner([*git, "merge-base", "--is-ancestor", "origin/main", "HEAD"])
    if contained.returncode == 0:
        say(f"already up to date (origin/main {target[:12]} is in HEAD).")
        return 0
    # Untracked files are expected (installed skills live untracked), but a
    # modified tracked file would be clobbered by -X theirs without warning.
    dirty = runner([*git, "status", "--porcelain", "--untracked-files=no"])
    if dirty.stdout.strip():
        say(
            f"uncommitted changes in {repo_dir} — commit or stash them first:\n"
            f"{dirty.stdout.strip()}"
        )
        return 1
    merge = runner(
        [*git, "merge", "-X", "theirs", "--no-edit", "origin/main"]
    )
    if merge.returncode != 0:
        runner([*git, "merge", "--abort"])
        say(f"merge failed, tree left untouched: {merge.stderr.strip()}")
        return 1
    sync = runner(["uv", "sync"])
    if sync.returncode != 0:
        say(f"uv sync failed: {sync.stderr.strip()}")
        return _rollback(git=git, runner=runner, service=service, to=before, say=say)
    synced, drifted = sync_installed_skills(repo_dir, runner=runner, before=before)
    if synced:
        say(f"re-synced installed skills: {', '.join(synced)}")
        # skills/ is tracked on a real install, so leaving the copies modified
        # would trip this command's own dirty-tree guard on the next run.
        runner([*git, "add", "skills"])
        runner([*git, "commit", "-m", "chore(skills): re-sync after update"])
    if drifted:
        say(
            f"NOT overwritten (locally edited): {', '.join(drifted)} — reconcile "
            f"against packages/ by hand."
        )
    if not service.installed:
        say("no autostart service installed — restart chief yourself.")
        say(f"updated {before[:12]} → {target[:12]}.")
        return 0
    service.restart()
    say("autostart service restarted.")
    check = healthy or (lambda: _is_live(service))
    if not check():
        say("daemon did not come back healthy — rolling back.")
        return _rollback(git=git, runner=runner, service=service, to=before, say=say)
    say(f"updated {before[:12]} → {target[:12]}.")
    return 0


def _is_live(service: ServiceManager) -> bool:
    """Both the service and the web port must answer.

    A web probe alone is not enough: right after a restart the *old* process
    can still be serving while the new one never comes up, so the probe passes
    and a dead daemon reports success. Asking the service manager first catches
    the label having been dropped entirely.
    """
    if service.status() in ("stopped", "not installed"):
        return False
    return wait_for_health(web_url(), timeout=45.0)


def _rollback(
    *,
    git: list[str],
    runner: Runner,
    service: ServiceManager,
    to: str,
    say: Callable[[str], None],
) -> int:
    """Restore the pre-update commit and restart. Best effort: if this fails
    too, say so loudly rather than pretending the box is fine."""
    reset = runner([*git, "reset", "--hard", to])
    runner(["uv", "sync"])
    if service.installed:
        service.restart()
    if reset.returncode != 0:
        say(f"ROLLBACK FAILED — {to[:12]} not restored: {reset.stderr.strip()}")
        return 1
    say(f"rolled back to {to[:12]}.")
    return 1
