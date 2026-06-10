"""Per-account credential registry for the multi-account Calendar server.

Loads all ``google_token*.json`` files from a token directory into a dict keyed
by account label (the ``account`` field from each token file), then provides
:func:`select_credential` to pick the right in-memory credential per request.

This module is **chief-internal** — it is NOT shipped in the docker/mcp-calendar
image (which has its own standalone ``server.py``).  It lives here so the
wiring layer (``tasks.py`` / ``_wire_owner_session``) can reason about which
credential a given label maps to, without importing docker container code.

The calendar container's own ``server.py`` has a mirror of this logic inline,
keeping the container image independent of the chief package.

Credential objects are typed as ``google.oauth2.credentials.Credentials`` at
runtime but referenced via a structural Protocol here so the chief package can
import this module without ``google-auth`` installed in the core image (the
container has it; the core image may not in certain test environments).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("chief.tools.calendar.credentials")

#: The legacy single-token filename (backward-compat primary account).
_LEGACY_NAME = "google_token.json"
#: Prefix shared by all token files.
_TOKEN_PREFIX = "google_token"
#: Header name the calendar server reads for per-request account selection.
ACCOUNT_HEADER = "X-Account-Label"


class _Credentials(Protocol):
    """The slice of google.oauth2.credentials.Credentials we store."""

    @property
    def expired(self) -> bool: ...

    @property
    def refresh_token(self) -> str | None: ...

    def refresh(self, request: Any) -> None: ...


def _load_one(token_path: Path) -> tuple[str | None, Any | None]:
    """Load one token file and return ``(label, credentials)`` or ``(None, None)``.

    Credentials are loaded from the JSON without performing a live refresh —
    the calendar server refreshes in memory on the first API call (the same
    behaviour as the single-account server). This avoids network calls at
    startup and in tests.
    """
    try:
        from google.oauth2.credentials import Credentials

        raw = json.loads(token_path.read_text(encoding="utf-8"))
        scopes = raw.get("scopes") or ["https://www.googleapis.com/auth/calendar"]
        creds = Credentials.from_authorized_user_info(raw, scopes=scopes)  # type: ignore[no-untyped-call]
        # Do NOT refresh here: the first API call will trigger an in-memory
        # refresh automatically (same behaviour as the original server.py
        # single-account load path where google-api-python-client refreshes on
        # each call).
        label: str | None = raw.get("account")
        if not label:
            # Fall back to filename slug (legacy token without ``account`` field).
            stem = token_path.stem  # e.g. "google_token" or "google_token_work"
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

    Each value is a ``google.oauth2.credentials.Credentials`` object refreshed
    in memory (no write-back) — ready for use with ``googleapiclient``.
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
    # Fallback: return the first registered credential (legacy / single-account).
    return next(iter(registry.values()))
