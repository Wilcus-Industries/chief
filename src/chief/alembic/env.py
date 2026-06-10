"""Alembic env.py — sync runner for chief's async-aiosqlite schema.

Alembic migration scripts always run synchronously (via run_sync or a sync engine),
even when the application uses an async engine at runtime.  We swap ``aiosqlite``
for the stdlib ``sqlite3`` driver only while running migrations so Alembic never
touches the event loop.  The application's ``init_db`` helper calls
``alembic upgrade head`` via the Config API with a sync URL derived from the
configured db_path.
"""

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from chief.persistence.models import Base

config = context.config

# Only honor alembic.ini's [logging] sections when run from the alembic CLI
# (bare root logger).  Programmatic runs (entrypoint / init_db at app boot)
# already have the app's JSON/stdout handler installed; fileConfig would
# replace it and reset the root level to WARNING, silently dropping every
# INFO log for the life of the process.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _sync_url(url: str) -> str:
    """Convert an aiosqlite URL to a plain sqlite URL for Alembic's sync runner."""
    return url.replace("sqlite+aiosqlite", "sqlite", 1)


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting (--sql mode)."""
    url = _sync_url(config.get_main_option("sqlalchemy.url", ""))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _sync_url(
        config.get_main_option("sqlalchemy.url", "")
    )
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
