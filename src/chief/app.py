"""Entrypoint — wire settings, logging, db, the task engine, and the adapter, then run.

Run with ``python -m chief.app``. Everything runs on one asyncio loop
(:func:`asyncio.run`): schema init, the engine's per-task turns/timers, and the
adapter's long-poll all share it. Restart recovery runs once the connection is live
(``on_ready``), so it can ping the owner about tasks left mid-flight.
"""

import asyncio
import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.ext import Application

from .adapters.telegram import TelegramAdapter, TelegramTaskIO
from .config import Settings
from .core.tasks import TaskManager
from .obs.logging import configure_logging
from .persistence.db import create_engine, init_db, session_factory

DOCKER_SECRETS_DIR = "/run/secrets"


def load_settings() -> Settings:
    """Load settings, using Docker secrets when mounted and env otherwise.

    Disabling the secrets source when ``/run/secrets`` is absent avoids a noisy
    "directory does not exist" warning on local runs, where tokens come from env.
    """
    if os.path.isdir(DOCKER_SECRETS_DIR):
        return Settings(_secrets_dir=DOCKER_SECRETS_DIR)  # type: ignore[call-arg]
    return Settings()  # type: ignore[call-arg]


def build_components(
    settings: Settings,
    *,
    application: Application,  # type: ignore[type-arg]
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[TaskManager, TelegramAdapter]:
    """Wire the engine and adapter against a built ``application`` (shared bot)."""
    manager = TaskManager(
        session_factory=session_factory,
        io=TelegramTaskIO(application.bot),
        owner_model=settings.owner_model_default,
        classifier_model=settings.classifier_model,
        concurrency=settings.concurrency,
        grace_seconds=settings.grace_seconds,
        idle_archive_seconds=settings.idle_archive_seconds,
    )
    adapter = TelegramAdapter(
        application=application,
        engine=manager,
        owner_id=settings.owner_telegram_id,
        guest_ack=settings.guest_ack,
        session_factory=session_factory,
    )
    return manager, adapter


async def serve(settings: Settings) -> None:
    """Bring up db + engine + adapter on one loop and run until stopped."""
    engine: AsyncEngine = create_engine(settings.db_path)
    await init_db(engine)
    factory = session_factory(engine)
    application = Application.builder().token(settings.telegram_bot_token).build()
    manager, adapter = build_components(
        settings, application=application, session_factory=factory
    )
    try:
        await adapter.run(on_ready=manager.recover)
    finally:
        await manager.shutdown()
        await engine.dispose()


def main() -> None:
    configure_logging()
    settings = load_settings()

    # The SDK's `claude` subprocess authenticates from CLAUDE_CODE_OAUTH_TOKEN in its
    # environment. Bridge the value here so it works whether the token arrived via a
    # Docker secret file or an env var. (config rejects ANTHROPIC_API_KEY, which would
    # otherwise outrank it and bill the API.)
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
