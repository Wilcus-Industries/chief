"""One-time Google OAuth consent -> a mountable token (Calendar/Drive/Sheets/Gmail).

The only interactive piece of the Google integration: the operator runs this once on
their dev machine (``python -m chief.tools.google.auth``), completes the loopback
consent in a browser, and gets a **google-auth Python credential** written to
``./secrets/google_tokens/google_token.json``. That single token is bind-mounted into
all five Google-consuming containers (core, mcp-calendar, mcp-drive, mcp-sheets,
mcp-gmail) from the same canonical subdirectory -- so the VPS never runs a consent
callback server (DESIGN: "run the consent dance once locally, mount the resulting
refresh token -- no VPS callback"). No manual move/copy step is required after minting.

The output is ``Credentials.to_json()`` (the native google-auth shape: ``refresh_token``
/ ``token`` / ``scopes`` / ``token_uri`` / ``client_id`` / ``client_secret``), which
every server reads with ``Credentials.from_authorized_user_file`` and refreshes in
memory. One token, four scopes -- no per-service files, no Node-shape remap.

``google-auth-oauthlib`` is a **dev/host-only** dependency (imported lazily): the
runtime containers reach Google through the MCP servers, not through this helper, so the
core image stays lean.
"""

import argparse
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .accounts import token_path_for

logger = logging.getLogger("chief.tools.google.auth")

#: Calendar R/W, Drive (read + upload), Sheets R/W, Gmail R/W (read + send/modify)
#: -- the union the four servers need. Delete is permitted by the calendar/gmail scopes
#: but each server keeps its permanent-delete tools deferred.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
)

#: Where the operator drops the Google Cloud "Desktop app" OAuth client, and where the
#: minted token lands.  ``DEFAULT_TOKEN_PATH`` writes directly into the canonical
#: ``secrets/google_tokens/`` subdirectory so docker-compose can bind-mount it
#: immediately — no manual move/copy step required (issue #58).
DEFAULT_CLIENT_PATH = Path("secrets/google_oauth_client.json")
DEFAULT_TOKEN_PATH = Path("secrets/google_tokens/google_token.json")

#: Google userinfo endpoint -- returns ``email`` (and profile data) for an access token.
_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo"

#: OAuth endpoints for the headless chat-consent add-account flow (issue #53). The
#: authorization endpoint builds the URL the owner opens in a browser; the token
#: endpoint exchanges the pasted code for a refresh token.
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Loopback redirect for the Desktop-app OAuth client. There is no callback server on
#: the VPS (DESIGN): the browser lands on an unreachable ``http://localhost`` page and
#: the owner copies the ``?code=...`` out of the address bar back into chat.
_LOOPBACK_REDIRECT = "http://localhost"


class _Credentials(Protocol):
    """The slice of ``google.oauth2.credentials.Credentials`` we serialize."""

    def to_json(self) -> str: ...


#: Run the consent dance for ``client_secrets`` over ``scopes`` -> fresh credentials.
ConsentFn = Callable[[Path, list[str]], _Credentials]
#: Optionally called after consent to retrieve the authenticated user's email address.
#: Injected in production; omitted when callers don't need the account label.
EmailFn = Callable[[], str]


class ClientSecretsMissing(FileNotFoundError):
    """The OAuth client-secrets file the operator must download first is absent."""


def _run_local_consent(client_secrets: Path, scopes: list[str]) -> _Credentials:
    """Default consent: a real loopback browser flow (needs google-auth-oauthlib)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=scopes)
    creds: _Credentials = flow.run_local_server(port=0)
    return creds


def _fetch_userinfo_email(credentials: _Credentials) -> str:
    """Fetch the authenticated user's email from the Google userinfo endpoint.

    Uses the access token embedded in ``credentials`` (available immediately after a
    successful consent flow).  Returns the ``email`` field from the JSON response.

    Raises :exc:`RuntimeError` when the request fails or the response has no
    ``email`` field so callers can decide whether to propagate or swallow.
    This is a **host-only** helper called from :func:`main` after consent; the core
    container never calls it.
    """
    token_data = json.loads(credentials.to_json())
    access_token = token_data.get("token")
    if not access_token:
        raise RuntimeError("credentials carry no access token; cannot fetch userinfo")

    req = urllib.request.Request(
        _USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data: dict[str, object] = json.loads(resp.read().decode())

    email = data.get("email")
    if not email:
        raise RuntimeError(f"userinfo response has no 'email' field: {data!r}")
    return str(email)


def mint_token(
    *,
    client_secrets: Path,
    token_out: Path,
    consent: ConsentFn | None = None,
    scopes: Sequence[str] = SCOPES,
    email_fn: EmailFn | None = None,
) -> Path:
    """Run consent and write the google-auth token to ``token_out``.

    ``consent`` is injected in tests; production uses :func:`_run_local_consent`
    (resolved late so a monkeypatch on the module global takes effect). The credentials
    are written verbatim via ``Credentials.to_json()`` -- the shape every server reads.

    ``email_fn`` is an optional callable that returns the authenticated user's email
    address (e.g. from the Google userinfo endpoint).  When provided its return value
    is written into the token JSON as the ``account`` field, which
    :func:`~chief.tools.google.accounts.discover_accounts` reads to label the account.
    Existing tokens minted without this field degrade gracefully in the registry.

    Raises :class:`ClientSecretsMissing` if the client file isn't present.
    """
    if not client_secrets.exists():
        raise ClientSecretsMissing(str(client_secrets))
    resolver = consent if consent is not None else _run_local_consent
    credentials = resolver(client_secrets, list(scopes))
    token_out.parent.mkdir(parents=True, exist_ok=True)
    token_json = credentials.to_json()
    if email_fn is not None:
        # Inject the email into the google-auth JSON so the account registry can
        # label this account without a network round-trip at read time.
        token_data = json.loads(token_json)
        token_data["account"] = email_fn()
        token_json = json.dumps(token_data)
    token_out.write_text(token_json, encoding="utf-8")
    return token_out


# --- Runtime add-account via chat consent (issue #53) -----------------------------
#
# The headless-friendly path: chief builds a consent URL, the owner consents in a
# browser and pastes the loopback redirect (or bare code) back into chat, and chief
# exchanges it for a refresh token stored as ``google_token_<email-slug>.json``. No
# callback server on the VPS; the owner never handles a token file. Both the
# code-exchange and the email lookup are injectable so the flow is unit-testable
# without a browser or a real Google account (mirrors the ``consent`` seam above).

#: Exchange a pasted authorization code for credentials. Injected in tests;
#: production uses :func:`_default_exchange`.
ExchangeFn = Callable[[str], _Credentials]
#: Resolve the authenticated email from freshly-exchanged credentials. Defaults to
#: :func:`_fetch_userinfo_email`; injected in tests to avoid a network call.
EmailFromCredentials = Callable[[_Credentials], str]


class _RawCredentials:
    """Minimal :class:`_Credentials` wrapping a google-auth-shaped token dict.

    The default exchange builds this from the raw token endpoint response so the
    written file matches what ``Credentials.from_authorized_user_info`` reads.
    """

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def to_json(self) -> str:
        return json.dumps(self._data)


def _load_client_id_secret(client_secrets: Path) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` from a Desktop-app OAuth client JSON.

    The file nests the credentials under ``installed`` (Desktop app) or ``web``;
    this reads whichever is present.
    """
    data = json.loads(client_secrets.read_text(encoding="utf-8"))
    block = data.get("installed") or data.get("web") or {}
    return str(block["client_id"]), str(block["client_secret"])


def build_consent_url(
    *,
    client_secrets: Path,
    scopes: Sequence[str] = SCOPES,
    redirect_uri: str = _LOOPBACK_REDIRECT,
) -> str:
    """Build the Google OAuth consent URL the owner opens in a browser.

    ``access_type=offline`` + ``prompt=consent`` force a refresh token even on a
    re-consent, so the minted account is durable. Raises
    :class:`ClientSecretsMissing` if the client file is absent.
    """
    if not client_secrets.exists():
        raise ClientSecretsMissing(str(client_secrets))
    client_id, _ = _load_client_id_secret(client_secrets)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    return _AUTH_URI + "?" + urllib.parse.urlencode(params)


def extract_auth_code(pasted: str) -> str:
    """Pull the authorization code out of what the owner pasted.

    Accepts either a bare code or the full loopback redirect URL
    (``http://localhost/?code=...&scope=...``) — the owner can copy the whole
    address bar without trimming.
    """
    text = pasted.strip()
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.lstrip("?")
        codes = urllib.parse.parse_qs(query).get("code")
        if codes:
            return codes[0]
    return text


def _default_exchange(
    client_secrets: Path, redirect_uri: str, scopes: Sequence[str]
) -> ExchangeFn:
    """Build the production exchange: a plain POST to Google's token endpoint.

    Uses ``urllib`` (already a core dependency) rather than google-auth-oauthlib so
    the core container stays lean — the heavier loopback-flow helper is host-only.
    """
    client_id, client_secret = _load_client_id_secret(client_secrets)

    def exchange(code: str) -> _Credentials:
        body = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode()
        req = urllib.request.Request(
            _TOKEN_URI,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token: dict[str, object] = json.loads(resp.read().decode())
        if not token.get("refresh_token"):
            raise RuntimeError(
                "token response carried no refresh_token; re-consent with "
                "prompt=consent (the account may already be authorized)"
            )
        scope = token.get("scope")
        return _RawCredentials(
            {
                "token": token.get("access_token"),
                "refresh_token": token["refresh_token"],
                "token_uri": _TOKEN_URI,
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": str(scope).split() if scope else list(scopes),
            }
        )

    return exchange


def add_account_from_code(
    *,
    pasted: str,
    secrets_dir: Path,
    client_secrets: Path = DEFAULT_CLIENT_PATH,
    scopes: Sequence[str] = SCOPES,
    redirect_uri: str = _LOOPBACK_REDIRECT,
    exchange: ExchangeFn | None = None,
    email_from_credentials: EmailFromCredentials | None = None,
) -> tuple[Path, str]:
    """Exchange a pasted code, label by email, and store the new account token.

    Returns ``(token_path, email)``. The token lands in ``secrets_dir`` as
    ``google_token_<email-slug>.json`` (gitignored), with the resolved email written
    into the ``account`` field so :func:`~chief.tools.google.accounts.discover_accounts`
    labels it without a network round-trip — making it immediately selectable via
    ``set_account`` with no restart.

    ``exchange`` and ``email_from_credentials`` are injected in tests; production
    defaults hit Google's token and userinfo endpoints.
    """
    resolver = exchange or _default_exchange(client_secrets, redirect_uri, scopes)
    resolve_email = email_from_credentials or _fetch_userinfo_email
    credentials = resolver(extract_auth_code(pasted))
    email = resolve_email(credentials)
    token_data = json.loads(credentials.to_json())
    token_data["account"] = email
    out = token_path_for(secrets_dir, email)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(token_data), encoding="utf-8")
    return out, email


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: mint the token, print the next step. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chief.tools.google.auth",
        description="Mint a shared Google refresh token via local OAuth consent.",
    )
    parser.add_argument("--client", type=Path, default=DEFAULT_CLIENT_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args(argv)

    # Run consent first (captures credentials) so we can attempt userinfo before
    # deciding whether to pass email_fn.  A consent failure (missing client) is fatal;
    # a userinfo failure (network, bad token) is non-fatal -- we still write the token
    # so the operator isn't locked out.
    if not args.client.exists():
        print(
            f"OAuth client file not found: {args.client}\n"
            "Download the Desktop-app OAuth client JSON from Google Cloud Console "
            f"and save it to {args.client}."
        )
        return 1

    try:
        credentials = _run_local_consent(args.client, list(SCOPES))
    except Exception as exc:  # noqa: BLE001
        print(f"OAuth consent failed: {exc}")
        return 1

    # Try to resolve the authenticated email -- non-fatal if unavailable.
    email: str | None = None
    try:
        email = _fetch_userinfo_email(credentials)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not fetch email from userinfo (token will lack 'account' field): %s",
            exc,
        )

    # Build a zero-argument callable that returns the captured email, or None when
    # the userinfo call failed (token is written without the account field).
    email_fn: EmailFn | None = (lambda e: lambda: e)(email) if email is not None else (
        None
    )

    try:
        out = mint_token(
            client_secrets=args.client,
            token_out=args.out,
            consent=lambda _c, _s: credentials,
            email_fn=email_fn,
        )
    except ClientSecretsMissing as exc:
        print(
            f"OAuth client file not found: {exc}\n"
            "Download the Desktop-app OAuth client JSON from Google Cloud Console "
            f"and save it to {args.client}."
        )
        return 1

    print(
        f"Wrote {out}. docker-compose bind-mounts it (from secrets/google_tokens/)"
        " into core, mcp-calendar, mcp-drive, mcp-sheets, and mcp-gmail."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
