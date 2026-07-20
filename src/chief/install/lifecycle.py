"""Lifecycle operations: health polling, updates, uninstall.

``update`` **merges** ``origin/main`` — it does not check anything out. A
running chief edits its own source (the self-edit pipeline commits to this very
repo), so every live install carries local commits and diverges from any tag
permanently. A tag checkout would drop that local layer out of the working tree
and silently roll the box back to the release; a merge keeps it. There are no
migrations to run (the schema is created at boot by the new tree's code).

Installed ``skills/<name>/SKILL.md`` are install-time COPIES of the packaged
originals, so an update re-syncs them — otherwise a package skill edit ships as
code while the agent goes on reading the stale copy.

The CLI wrapping these lives in :mod:`.commands`.
"""

import shutil
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from chief.config import load_config
from chief.install.service import ServiceManager
from chief.install.units import Runner

DEFAULT_LAUNCHER = Path.home() / ".local" / "bin" / "chief"


def web_url(port: int | None = None) -> str:
    config = load_config()
    return f"http://127.0.0.1:{port or config.web_port}/"


def wait_for_health(url: str, *, timeout: float, interval: float = 0.25) -> bool:
    """Poll until any served answer (< 500) arrives — a login redirect counts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                url,
                timeout=min(2.0, max(0.1, deadline - time.monotonic())),
                follow_redirects=False,
            )
            if response.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(max(0.0, min(interval, deadline - time.monotonic())))
    return False


def sync_installed_skills(repo_dir: Path) -> list[str]:
    """Re-copy packaged SKILL.md files over their installed copies.

    Only skills already present in ``skills/`` are touched — installing a
    package is a separate, agent-driven step, and an update must never enable
    a capability the owner did not ask for. Returns the names re-synced.
    """
    synced: list[str] = []
    for source in sorted(repo_dir.glob("packages/*/skills/*/SKILL.md")):
        target = repo_dir / "skills" / source.parent.name / "SKILL.md"
        if not target.parent.is_dir():
            continue
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)
            synced.append(source.parent.name)
    return synced


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
    if before and before == target:
        say(f"already up to date ({before[:12]}).")
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
    synced = sync_installed_skills(repo_dir)
    if synced:
        say(f"re-synced installed skills: {', '.join(synced)}")
    if not service.installed:
        say("no autostart service installed — restart chief yourself.")
        say(f"updated {before[:12]} → {target[:12]}.")
        return 0
    service.stop()
    service.start()
    say("autostart service restarted.")
    check = healthy or (lambda: wait_for_health(web_url(), timeout=45.0))
    if not check():
        say("daemon did not come back healthy — rolling back.")
        return _rollback(git=git, runner=runner, service=service, to=before, say=say)
    say(f"updated {before[:12]} → {target[:12]}.")
    return 0


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
        service.stop()
        service.start()
    if reset.returncode != 0:
        say(f"ROLLBACK FAILED — {to[:12]} not restored: {reset.stderr.strip()}")
        return 1
    say(f"rolled back to {to[:12]}.")
    return 1


def uninstall(
    *,
    service: ServiceManager,
    launcher: Path,
    repo_dir: Path,
    purge_data: bool,
    assume_yes: bool,
    confirm: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Remove service + launcher; ``purge_data`` also deletes data/secrets."""
    if purge_data and not assume_yes:
        answer = confirm(
            f"Delete {repo_dir / 'data'} and {repo_dir / 'secrets'} too? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            say("aborted — nothing removed.")
            return 1
    service.uninstall()
    launcher.unlink(missing_ok=True)
    say("service + launcher removed.")
    if purge_data:
        shutil.rmtree(repo_dir / "data", ignore_errors=True)
        shutil.rmtree(repo_dir / "secrets", ignore_errors=True)
        say("data + secrets removed.")
    else:
        say("data + secrets kept (pass --purge-data to remove them).")
    return 0
