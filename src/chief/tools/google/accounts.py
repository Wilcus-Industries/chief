"""Google account registry — discover registered accounts from the token store.

Scans a secrets directory for ``google_token*.json`` files and returns a list of
:class:`GoogleAccount` entries.  The **legacy** single-token file
(``google_token.json``) auto-registers as the first account; additional accounts
live alongside it as ``google_token_<label>.json``.

Label assignment:
- ``google_token.json`` → label = email if present, else ``"google_token"``
- ``google_token_<label>.json`` → label = ``<label>`` slug from the filename

Email is read from the ``account`` field of the google-auth token JSON (written by
the updated :func:`~chief.tools.google.auth.mint_token`).  Tokens minted before
this field was added gracefully degrade: when an ``email_resolver`` is provided,
it is called with the token path and may return the email via a lightweight OAuth
token exchange + userinfo call; otherwise the label falls back to the filename slug.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("chief.tools.google.accounts")

#: The legacy single-token filename (backward-compat primary account).
_LEGACY_NAME = "google_token.json"
#: Prefix shared by all token files.
_TOKEN_PREFIX = "google_token"

#: Called with a token path to resolve the email for tokens that lack the
#: ``account`` field.  Returns the email string or ``None`` on failure.
#: Injected at discovery time; defaults to ``None`` (no resolution).
EmailResolver = Callable[[Path], "str | None"]


@dataclass(frozen=True)
class GoogleAccount:
    """One registered Google account entry."""

    #: Human-readable label: the email (new mints) or filename slug (legacy).
    label: str
    #: Google email address, or ``None`` for tokens minted before the account field.
    email: str | None


def _read_email(token_path: Path) -> str | None:
    """Return the ``account`` (email) from a google-auth token JSON, or ``None``."""
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
        value = data.get("account")
        return str(value) if value else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("failed to read token %s: %s", token_path, exc)
        return None


def _slug_from_name(filename: str) -> str:
    """Extract the label slug from a token filename (without extension).

    ``google_token.json`` → ``"google_token"``
    ``google_token_work.json`` → ``"work"``
    """
    stem = Path(filename).stem  # strip .json
    if stem == _TOKEN_PREFIX:
        return stem
    # strip the leading "google_token_" prefix
    prefix = _TOKEN_PREFIX + "_"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return stem


def _to_account(
    token_path: Path,
    email_resolver: EmailResolver | None = None,
) -> GoogleAccount:
    """Build a :class:`GoogleAccount` from one token file.

    When the token lacks an ``account`` field, ``email_resolver`` is called (if
    provided) to attempt a backward-compat resolution (e.g. via the Google userinfo
    endpoint).  If resolution fails or no resolver is given, the label falls back to
    the filename slug.
    """
    email = _read_email(token_path)
    if email is None and email_resolver is not None:
        try:
            email = email_resolver(token_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "email resolver failed for %s: %s", token_path, exc
            )
            email = None
    slug = _slug_from_name(token_path.name)
    # For both the legacy file and labeled files: if we have an email, use it as
    # the label (canonical).  If not, fall back to the filename slug so the entry
    # is still useful.
    label = email if email is not None else slug
    return GoogleAccount(label=label, email=email)


def discover_accounts(
    secrets_dir: Path,
    email_resolver: EmailResolver | None = None,
) -> list[GoogleAccount]:
    """Scan ``secrets_dir`` for google token files and return the account list.

    Returns an empty list when the directory does not exist or contains no token
    files.  The legacy ``google_token.json`` is always first when present;
    additional ``google_token_<label>.json`` files follow in alphabetical order.

    ``email_resolver`` is called for tokens that lack an ``account`` field (legacy
    tokens minted before issue #54's fix).  It receives the token path and should
    return the email string or ``None``.  When ``None``, legacy tokens degrade to
    a filename-slug label as before.  The production default wires a real OAuth
    token exchange + userinfo call via :func:`~chief.app.build_list_accounts_service`.
    """
    if not secrets_dir.is_dir():
        return []

    legacy: list[GoogleAccount] = []
    labeled: list[GoogleAccount] = []

    for path in sorted(secrets_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.name.startswith(_TOKEN_PREFIX):
            continue
        account = _to_account(path, email_resolver=email_resolver)
        if path.name == _LEGACY_NAME:
            legacy.append(account)
        else:
            labeled.append(account)

    return legacy + labeled
