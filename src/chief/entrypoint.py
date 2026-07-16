"""Daemon entrypoint: build the app from config and serve until signalled."""

import asyncio
import logging
import signal
from pathlib import Path

from chief.app import build_app
from chief.config import load_config
from chief.selfedit.recovery import clear_marker, restart_daemon, rollback_if_marked

logger = logging.getLogger(__name__)


async def amain() -> None:
    """Run the daemon until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    repo_root = Path.cwd()
    config = load_config()
    try:
        app = await build_app(config)
        await app.start()
    except Exception:
        # A failed boot right after a self-edit rolls back and re-execs.
        if rollback_if_marked(repo_root):
            restart_daemon()
        raise
    clear_marker(repo_root)
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
