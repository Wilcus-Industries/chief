"""Daemon entrypoint: build the app from config and serve until signalled."""

import asyncio
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from chief.config.history import snapshot
from chief.install.basepin import resolve_pending
from chief.install.migrate import migrate_instance
from chief.instance_lock import AlreadyRunning, acquire_instance_lock
from chief.selfedit.notice import mark_notice_rolled_back, take_restart_notice
from chief.selfedit.recovery import clear_marker, restart_daemon, rollback_if_marked

if TYPE_CHECKING:  # the runtime import stays inside the seatbelt below.
    from chief.daemon import App

logger = logging.getLogger(__name__)


async def report_restart(app: "App", repo_root: Path) -> None:
    """Tell the thread that asked for the restart that chief is back.

    Runs once the adapters are serving, so the owner does not have to poke
    the thread to find out whether the daemon returned. Best effort: a
    missing channel or a dead adapter must not take the fresh boot down.
    """
    notice = take_restart_notice(repo_root)
    if notice is None:
        return
    try:
        adapter = app.dispatcher.adapter(notice.channel)
        await adapter.send(notice.thread_key, notice.text())
    except Exception:
        logger.exception("could not report the restart on %s", notice.channel)


def record_config(config_path: Path, data_dir: Path) -> None:
    """Keep a copy of the config this boot came up on (best effort).

    Called only once the boot is proven, so the history holds configs that
    actually work — the previous entry is what a bad config write gets
    restored from, since git cannot roll back a file it does not track.
    A failure here must never take down an otherwise healthy boot.
    """
    try:
        written = snapshot(config_path, data_dir / "config-history", datetime.now(UTC))
    except OSError:
        logger.exception("could not snapshot config.yaml")
        return
    if written is not None:
        logger.info("config changed since the last boot; kept a copy at %s", written)


async def amain() -> None:
    """Run the daemon until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    repo_root = Path.cwd()
    try:
        # Imported inside the seatbelt: a self-edit that breaks chief.app's
        # import (yet somehow passes the done-check) still rolls back instead
        # of crashing before main() runs.
        from chief.app import build_app
        from chief.config import load_config

        # Before anything is served: the one-time crossover onto the
        # release-based update system (untrack installed skills, seed core's
        # own, record the base pin). Idempotent, and a no-op once done.
        for step in migrate_instance(repo_root):
            logger.info("update migration: %s", step)
        config = load_config()
        # One daemon per data dir: a stray second instance would double-poll
        # chat.db and answer every iMessage twice. Held for the whole process.
        _lock = acquire_instance_lock(config.db_path.parent / "chief.lock")
        app = await build_app(config)
        await app.start()
    except AlreadyRunning:
        logger.error("another chief instance is already running — refusing to start")
        raise SystemExit(1) from None
    except Exception:
        # A failed boot right after a self-edit rolls back and re-execs.
        if rollback_if_marked(repo_root):
            # The next boot reports the rollback on the requesting thread
            # instead of a "restart success" that never happened.
            mark_notice_rolled_back(repo_root)
            restart_daemon()
        raise
    clear_marker(repo_root)
    record_config(repo_root / "config.yaml", config.db_path.parent)
    # The box is up on the new code, so an update that landed is now proven.
    # Only here does the base pin advance; a rollback or an abandoned update
    # leaves HEAD where it was and the pin unmoved.
    if (version := resolve_pending(repo_root)) is not None:
        logger.info("now running %s", version)
    await report_restart(app, repo_root)
    logger.info("chief up — socket at %s", config.socket_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await app.stop()
    logger.info("chief stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
