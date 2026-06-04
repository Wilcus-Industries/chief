"""Resolve the owner's timezone for the Calendar MCP server.

This standalone image imports nothing from the chief package, so it can't read chief's
pydantic ``config.yaml`` directly — yet ``config.yaml`` is the single source of truth for
``owner_tz`` (chief-core reads the same key). The compose file mounts ``config.yaml``
read-only into the container and this module parses the one scalar out of it, with an
``OWNER_TZ`` env override on top (matching chief's env > config.yaml precedence) and a UTC
fallback. Pure stdlib (``re``/``zoneinfo``) so it is unit-testable by importlib-by-path,
like ``row1_guard`` and ``drive_query`` in the sibling images.
"""

import re
from zoneinfo import ZoneInfo

# Top-level (column-0) ``owner_tz:`` line; nested/indented keys never match. Captures the
# value up to an optional inline ``# comment``.
_OWNER_TZ_LINE = re.compile(r"^owner_tz:\s*([^#\n]*)")


def owner_tz_from_config(path: str) -> str | None:
    """Return the ``owner_tz`` value from a chief ``config.yaml``, or None.

    Reads only the first top-level ``owner_tz:`` line, strips an inline comment and
    surrounding quotes. Returns None when the key is absent or the file can't be read — no
    PyYAML dependency for a single scalar.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = _OWNER_TZ_LINE.match(line)
                if m:
                    value = m.group(1).strip().strip("'\"").strip()
                    return value or None
    except OSError:
        return None
    return None


def resolve_owner_tz(env_value: str | None, config_path: str) -> str:
    """Pick the timezone: non-empty env override → config.yaml → ``"UTC"``.

    The chosen zone is validated with ``ZoneInfo``; an unknown name (from either source)
    falls back to ``"UTC"`` so a typo can't crash module load.
    """
    candidate = (env_value or "").strip() or owner_tz_from_config(config_path) or "UTC"
    try:
        ZoneInfo(candidate)
    except (KeyError, ValueError, OSError):
        return "UTC"
    return candidate
