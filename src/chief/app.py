"""Entrypoint — wire settings, logging, db, and the Telegram adapter, then poll.

Run with ``python -m chief.app``. One-time async setup (schema init) runs under a
throwaway loop before the adapter takes over the event loop via long-polling.
"""

import asyncio
import os

from .adapters.telegram import TelegramAdapter
from .config import Settings
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


def main() -> None:
    configure_logging()
    settings = load_settings()

    # The SDK's `claude` subprocess authenticates from CLAUDE_CODE_OAUTH_TOKEN in its
    # environment. Bridge the value here so it works whether the token arrived via a
    # Docker secret file or an env var. (config rejects ANTHROPIC_API_KEY, which would
    # otherwise outrank it and bill the API.)
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token

    engine = create_engine(settings.db_path)
    asyncio.run(init_db(engine))

    adapter = TelegramAdapter(
        token=settings.telegram_bot_token,
        owner_id=settings.owner_telegram_id,
        owner_model=settings.owner_model_default,
        guest_ack=settings.guest_ack,
        session_factory=session_factory(engine),
    )
    adapter.run()


if __name__ == "__main__":
    main()
