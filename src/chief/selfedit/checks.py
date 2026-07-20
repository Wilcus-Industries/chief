"""What a restart must satisfy before it is allowed to commit and reboot.

Two gates, both run by the pipeline: the project done-check against the
working tree, and a load of the *live* ``config.yaml``. The second exists
because the done-check runs against fixtures — a bad real config value passes
every test and then boot-loops launchd, and a config-only change writes no
rollback marker for the boot side to undo.
"""

import asyncio
from collections.abc import Callable

# The project done-check (CLAUDE.md); injectable so tests use a fast stand-in.
DEFAULT_CHECKS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "pytest", "-q"),
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "mypy", "."),
)


async def validate_live_config(validate: Callable[[], object]) -> str | None:
    """Load the live config off the event loop; return the error on failure.

    Any load exception aborts the restart — this pre-restart gate is the only
    prevention for a bad-config reboot, since git can't rewind a gitignored
    ``config.yaml``.
    """
    try:
        await asyncio.to_thread(validate)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None
