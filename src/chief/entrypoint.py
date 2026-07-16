"""Daemon entrypoint: build the app from config and serve until signalled."""

import asyncio
import logging
import signal

from chief.app import build_app
from chief.config import load_config

logger = logging.getLogger(__name__)


async def amain() -> None:
    """Run the daemon until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = load_config()
    app = await build_app(config)
    await app.start()
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
