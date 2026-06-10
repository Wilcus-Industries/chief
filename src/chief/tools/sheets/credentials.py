"""Per-account credential registry and atomic token write-back for Sheets.

Mirrors ``chief.tools.calendar.credentials`` for loading and selection, and adds
:func:`write_token_atomic` for the Sheets-specific requirement: Sheets is the sole
writer of token files, and under multi-account each account's refresh token must
persist to its own file atomically (write-temp-then-rename) so:

- Concurrent writes to *different* account files never collide (each targets its
  own file; rename is atomic at the OS level).
- A reader of one account's file never sees a partial write (the rename swaps in
  the complete new file in one operation).

This module is **chief-internal** — it is NOT shipped in the docker/mcp-sheets
image (which has its own standalone ``server.py``).  The container's server.py
replicates the atomic-write logic directly to keep the image independent of the
chief package.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("chief.tools.sheets.credentials")

#: The legacy single-token filename (backward-compat primary account).
_LEGACY_NAME = "google_token.json"
#: Prefix shared by all token files.
_TOKEN_PREFIX = "google_token"
#: Header name the sheets server reads for per-request account selection.
ACCOUNT_HEADER = "X-Account-Label"


def _load_one(token_path: Path) -> tuple[str | None, Any | None]:
    """Load one token file and return ``(label, credentials)`` or ``(None, None)``.

    Credentials are loaded without performing a live refresh.
    """
    try:
        from google.oauth2.credentials import Credentials

        raw = json.loads(token_path.read_text(encoding="utf-8"))
        scopes = raw.get("scopes") or [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
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
    registry is empty.
    """
    if not registry:
        return None
    if label and label in registry:
        return registry[label]
    return next(iter(registry.values()))


def write_token_atomic(token_path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as JSON to ``token_path`` atomically (write-temp-then-rename).

    Uses :func:`tempfile.NamedTemporaryFile` in the same directory as
    ``token_path`` so the final ``os.replace`` is guaranteed to be an
    intra-filesystem rename (atomic at the POSIX level).  A reader of
    ``token_path`` therefore never sees a partial write — it sees either the
    old complete file or the new complete file, never a mix.

    Concurrent writes to *different* account files are safe by construction:
    each targets its own path, so their renames don't interfere.  Concurrent
    writes to the *same* file are serialised by the OS rename (last writer
    wins, but never produces a corrupt file).
    """
    text = json.dumps(data, ensure_ascii=False)
    parent = token_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # delete=False so we can rename it; we clean up on error.
    fd, tmp_name = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp_name).replace(token_path)
    except Exception:
        # Best-effort cleanup of the temp file; ignore secondary errors.
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise
