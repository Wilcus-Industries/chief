"""Daemon entrypoint: wire the core together and serve until signalled."""

import asyncio
import logging
import signal

from chief.adapters.socket import SocketAdapter
from chief.agent.manager import SessionManager
from chief.agent.prompt import system_prompt
from chief.agent.tools import ToolRegistry
from chief.config import Config, load_config
from chief.dispatch import Dispatcher
from chief.persistence.db import init_schema, make_engine, make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.openrouter import OpenRouterProvider

logger = logging.getLogger(__name__)


async def build_dispatcher(config: Config, store: MessageStore) -> Dispatcher:
    """Assemble provider, registry, sessions, and dispatcher from config."""
    provider = OpenRouterProvider(config.openrouter_api_key)
    registry = ToolRegistry()
    manager = SessionManager(
        provider=provider,
        registry=registry,
        store=store,
        default_model=config.default_model,
        system_prompt=system_prompt(),
        max_concurrent=config.max_concurrent_sessions,
    )
    return Dispatcher(manager)


async def amain() -> None:
    """Run the daemon until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = load_config()
    engine = make_engine(config.db_path)
    await init_schema(engine)
    store = MessageStore(make_session_factory(engine))
    dispatcher = await build_dispatcher(config, store)
    socket_adapter = SocketAdapter(config.socket_path, dispatcher.handle)
    dispatcher.register(socket_adapter)
    await socket_adapter.start()
    logger.info("chief up — socket at %s", config.socket_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await socket_adapter.stop()
    await engine.dispose()
    logger.info("chief stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
