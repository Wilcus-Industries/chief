"""Per-account credential registry for the multi-account Drive server.

Mirrors ``chief.tools.calendar.credentials`` exactly — same loading and selection
logic, Drive-specific scopes.  Kept separate so Drive's scopes stay independent
of Calendar's, and so each module has a single reason to exist.

This module is **chief-internal** — it is NOT shipped in the docker/mcp-drive
image (which has its own standalone ``server.py``).  It lives here so the
wiring layer can reason about which credential a given label maps to, without
importing docker container code.

The Drive container's own ``server.py`` has equivalent logic inline,
keeping the container image independent of the chief package.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("chief.tools.drive.credentials")

#: The legacy single-token filename (backward-compat primary account).
_LEGACY_NAME = "google_token.json"
#: Prefix shared by all token files.
_TOKEN_PREFIX = "google_token"
#: Header name the drive server reads for per-request account selection.
ACCOUNT_HEADER = "X-Account-Label"


def _load_one(token_path: Path) -> tuple[str | None, Any | None]:
    """Load one token file and return ``(label, credentials)`` or ``(None, None)``.

    Credentials are loaded without performing a live refresh — the drive server
    refreshes in memory on the first API call.
    """
    try:
        from google.oauth2.credentials import Credentials

        raw = json.loads(token_path.read_text(encoding="utf-8"))
        scopes = raw.get("scopes") or ["https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_authorized_user_info(raw, scopes=scopes)  # type: ignore[no-untyped-call]
        label: str | None = raw.get("account")
        if not label:
            stem = token_path.stem
            prefix = _TOKEN_PREFIX + "_"
            label = stem[len(prefix):] if stem.startswith(prefix) else stem
        return label, creds
    except Exception:  # noqa: BLE001
        logger.warning("failed to load token %s", token_path, exc_info=True)
        return None, None


def load_account_credentials(token_dir: Path) -> dict[str, Any]:
    """Scan ``token_dir`` for ``google_token*.json`` files and return a label→creds map.

    The legacy ``google_token.json`` is always loaded first; additional
    ``google_token_<label>.json`` files follow in alphabetical order.  Returns an
    empty dict when the directory is absent or contains no token files.
    """
    if not token_dir.is_dir():
        return {}

    registry: dict[str, Any] = {}
    legacy: list[tuple[str, Any]] = []
    labeled: list[tuple[str, Any]] = []

    for path in sorted(token_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.name.startswith(_TOKEN_PREFIX):
            continue
        label, creds = _load_one(path)
        if label is None or creds is None:
            continue
        if path.name == _LEGACY_NAME:
            legacy.append((label, creds))
        else:
            labeled.append((label, creds))

    for label, creds in legacy + labeled:
        registry[label] = creds

    return registry


def select_credential(
    registry: dict[str, Any],
    label: str | None,
) -> Any | None:
    """Return the credential for ``label``, or the default (first) credential.

    Falls back to the first registered credential when ``label`` is ``None`` or
    not found in the registry (backward compat: a single-account deployment
    never sets a label header and still works).  Returns ``None`` only when the
    registry is empty (no credentials configured at all).
    """
    if not registry:
        return None
    if label and label in registry:
        return registry[label]
    return next(iter(registry.values()))
