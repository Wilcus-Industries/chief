"""Container entrypoint: run Alembic migrations then exec the app.

This module is the container's ENTRYPOINT wrapper. It runs ``alembic upgrade head``
(fail-loud: non-zero exit + clear log on failure) before handing control to the app
via ``os.execv``. The app never boots on a half-migrated schema.

Invoke as the container entrypoint::

    python -m chief.entrypoint

Or import and call :func:`migrate_then_exec` directly (used by tests and __main__).
"""

import logging
import os
import sys

from .persistence.db import _run_migrations

logger = logging.getLogger("chief.entrypoint")


def migrate_then_exec(db_path: str, app_argv: list[str]) -> None:
    """Run ``alembic upgrade head`` then exec the app process.

    On success, replaces the current process with ``app_argv`` via ``os.execv``
    — this function does not return. On failure, logs a clear error and calls
    ``sys.exit(1)``; the app is never started.

    Args:
        db_path: Filesystem path to the SQLite database file.
        app_argv: The command + arguments to exec on success (e.g.
            ``[sys.executable, "-m", "chief.app"]``).
    """
    try:
        logger.info("running alembic upgrade head against %s", db_path)
        _run_migrations(db_path)
        # alembic's env.py calls fileConfig(alembic.ini) during the migration,
        # which resets the root logger level to WARNING and replaces our JSON/stdout
        # handler with a plain stderr one — so any INFO log after this point would
        # be silently dropped.  Restore our logging config before the completion
        # line so it reaches container logs as intended.
        from .obs.logging import configure_logging

        configure_logging()
        logger.info("migration complete — starting app")
    except Exception as exc:
        logger.error(
            "migration failed — aborting startup to prevent a half-migrated schema: %s",
            exc,
        )
        sys.exit(1)

    # Flush stdout before execv: os.execv replaces the process image without
    # flushing Python's internal I/O buffers, so any log line written just before
    # execv would be silently lost. We flush explicitly so the migration-complete
    # line is visible in container logs.
    sys.stdout.flush()
    os.execv(app_argv[0], app_argv)


def main() -> None:
    """Entrypoint: read DB_PATH from the environment, migrate, then exec the app."""
    from .obs.logging import configure_logging

    configure_logging()

    # DB_PATH mirrors the Docker volume mount used in docker-compose.yml (sqlite-data).
    # Falls back to /data/chief.db — the same default used by config.py.
    db_path = os.environ.get("DB_PATH", "/data/chief.db")
    app_argv = [sys.executable, "-m", "chief.app"]
    migrate_then_exec(db_path, app_argv)


if __name__ == "__main__":
    main()
