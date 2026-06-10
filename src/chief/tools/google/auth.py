"""One-time Google OAuth consent → a mountable token (Calendar/Drive/Sheets/Gmail).

The only interactive piece of the Google integration: the operator runs this once on
their dev machine (``python -m chief.tools.google.auth``), completes the loopback
consent in a browser, and gets a **google-auth Python credential** written to
``./secrets/google_token.json``. That single token is bind-mounted into all four Google
MCP containers — so the VPS never runs a consent callback server (DESIGN: "run the
consent dance once locally, mount the resulting refresh token — no VPS callback").

The output is ``Credentials.to_json()`` (the native google-auth shape: ``refresh_token``
/ ``token`` / ``scopes`` / ``token_uri`` / ``client_id`` / ``client_secret``), which
every server reads with ``Credentials.from_authorized_user_file`` and refreshes in
memory. One token, four scopes — no per-service files, no Node-shape remap.

``google-auth-oauthlib`` is a **dev/host-only** dependency (imported lazily): the
runtime containers reach Google through the MCP servers, not through this helper, so the
core image stays lean.
"""

import argparse
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

#: Calendar R/W, Drive (read + upload), Sheets R/W, Gmail R/W (read + send/modify) — the
#: union the four servers need. Delete is permitted by the calendar/gmail scopes but
#: each server keeps its permanent-delete tools deferred.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.modify",
)

#: Where the operator drops the Google Cloud "Desktop app" OAuth client, and where the
#: minted token lands — both under ``./secrets/`` so docker-compose mounts them.
DEFAULT_CLIENT_PATH = Path("secrets/google_oauth_client.json")
DEFAULT_TOKEN_PATH = Path("secrets/google_token.json")


class _Credentials(Protocol):
    """The slice of ``google.oauth2.credentials.Credentials`` we serialize."""

    def to_json(self) -> str: ...


#: Run the consent dance for ``client_secrets`` over ``scopes`` → fresh credentials.
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
    are written verbatim via ``Credentials.to_json()`` — the shape every server reads.

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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: mint the token, print the next step. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chief.tools.google.auth",
        description="Mint a shared Google refresh token via local OAuth consent.",
    )
    parser.add_argument("--client", type=Path, default=DEFAULT_CLIENT_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args(argv)

    try:
        out = mint_token(client_secrets=args.client, token_out=args.out)
    except ClientSecretsMissing as exc:
        print(
            f"OAuth client file not found: {exc}\n"
            "Download the Desktop-app OAuth client JSON from Google Cloud Console "
            f"and save it to {args.client}."
        )
        return 1

    print(
        f"Wrote {out}. docker-compose bind-mounts it into the mcp-calendar, mcp-drive, "
        "mcp-sheets, and mcp-gmail containers as their shared token store."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
