"""Per-value coercion for the config load path.

Split out of ``load.py`` so that file stays the field table and this one holds
the "what shapes are acceptable, and what is a foot-gun worth refusing at
boot" judgements. Everything here raises :class:`ConfigError` rather than
coercing a value whose wrong reading would fail silently later.
"""

import os
from pathlib import Path
from typing import Any

from chief.config.schema import AliasSpec, BackendSpec, ConfigError

AUTONOMY_VALUES = ("off", "clean-only", "full")


def read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def as_handles(value: Any) -> tuple[str, ...]:
    """Coerce ``imessage.owner_handles`` into a tuple of strings.

    A single quoted string becomes a one-element tuple; a list/tuple becomes
    per-element strings; missing/empty becomes ``()``. A **bare numeric scalar**
    (``owner_handles: +15551234567`` → YAML parses it as the int
    ``15551234567``, silently dropping the ``+``) is rejected with a clear
    ``ConfigError``: coercing it would scope to the wrong chat and fail
    silently, and raising turns the mini boot-loop (``tuple(int)`` TypeError
    deep in boot) into an actionable error. A mapping is likewise rejected.
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    raise ConfigError(
        "imessage.owner_handles must be a quoted handle or a list of quoted "
        f"handles (e.g. [\"+15551234567\"]), got {type(value).__name__} "
        f"{value!r} — quote it so YAML keeps the leading '+'."
    )


def autonomy(value: Any) -> str:
    """Coerce ``update.autonomy``; reject anything outside the three values.

    A typo must not read as permission — nor as a silent ``off``. Left to
    coerce, an unknown value would decide unattended conflict resolution by
    accident, so it fails the restart gate instead.

    YAML reads a bare ``off`` as the boolean ``False``, which is exactly what
    the owner meant, so that one is accepted. A bare ``on``/``true`` names no
    autonomy level at all and is refused rather than guessed at.
    """
    if isinstance(value, bool):
        if not value:
            return "off"
        raise ConfigError(
            "update.autonomy must be off, clean-only, or full — YAML read a "
            "bare on/true, which names no level; quote the value you meant."
        )
    parsed = str(value).strip()
    if parsed not in AUTONOMY_VALUES:
        raise ConfigError(
            "update.autonomy must be "
            f"{', '.join(AUTONOMY_VALUES)} — got {parsed!r}"
        )
    return parsed


def backends(raw: dict[str, Any]) -> dict[str, BackendSpec]:
    return {
        name: BackendSpec(base_url=str(spec["base_url"]), api_key=_backend_key(spec))
        for name, spec in raw.items()
    }


def _backend_key(spec: dict[str, Any]) -> str:
    """Resolve a backend key: ``api_key_env`` var first, then a secret file."""
    env = spec.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env]
    if secret := spec.get("api_key_secret"):
        return read_secret(Path("secrets") / str(secret))
    return ""


def aliases(raw: dict[str, Any]) -> dict[str, AliasSpec]:
    return {
        name: AliasSpec(backend=str(spec["backend"]), model=str(spec["model"]))
        for name, spec in raw.items()
    }


def windows(raw: dict[str, Any]) -> dict[str, int]:
    """Coerce the ``compaction.windows`` override map to ``model-name -> tokens``."""
    return {str(name): int(tokens) for name, tokens in raw.items()}


def ratio(raw: Any) -> float:
    """Coerce ``compaction.ratio`` and clamp it to ``(0, 1]``.

    ``0`` (or negative) would fire compaction every turn; ``>1`` would silently
    disable threshold compaction — both foot-guns, so reject at boot instead.
    """
    parsed = float(raw)
    if not 0 < parsed <= 1:
        raise ConfigError(f"compaction.ratio must be in (0, 1], got {parsed}")
    return parsed
