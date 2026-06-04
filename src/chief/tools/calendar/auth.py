"""One-time Google Calendar OAuth consent → a mountable refresh token.

The only interactive piece of M5: the operator runs this once on their dev machine
(``python -m chief.tools.calendar.auth``), completes the loopback consent in a browser,
and gets **nspady's Node token file** (account ``normal``) written to
``./secrets/google_calendar_token.json``. That file is bind-mounted into the
``mcp-gcal`` container at its token path — so the VPS never runs a consent callback
server (DESIGN: "run the consent dance once locally, mount the resulting refresh token
— no callback server on the VPS").

The output is shaped for ``nspady/google-calendar-mcp``: google-auth's Python
``Credentials`` (``token`` / ``scopes`` / ``expiry``) is remapped to the Node
google-auth-library credential (``access_token`` / ``scope`` / ``expiry_date``) and
pre-wrapped under the ``normal`` account key, so the server loads it as-is instead of
rewriting a single-account file on first boot (see :func:`_to_node_token`).

``google-auth-oauthlib`` is a **dev/host-only** dependency (imported lazily): the
runtime container reaches Google through the MCP server, not through this helper, so the
core image stays lean.
"""

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

#: Calendar R/W + free/busy + list-calendars (multi-calendar). Delete is permitted by
#: the scope but deliberately unwired in M5 (deferred — see the M5 plan / DESIGN note).
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar",)

#: Where the operator drops the Google Cloud "Desktop app" OAuth client, and where the
#: minted token lands — both under ``./secrets/`` so docker-compose mounts them.
DEFAULT_CLIENT_PATH = Path("secrets/google_oauth_client.json")
DEFAULT_TOKEN_PATH = Path("secrets/google_calendar_token.json")


class _Credentials(Protocol):
    """The slice of ``google.oauth2.credentials.Credentials`` we serialize."""

    token: str | None
    refresh_token: str | None
    scopes: Sequence[str] | None
    expiry: datetime | None  # naive UTC, per google-auth


#: Run the consent dance for ``client_secrets`` over ``scopes`` → fresh credentials.
ConsentFn = Callable[[Path, list[str]], _Credentials]


class ClientSecretsMissing(FileNotFoundError):
    """The OAuth client-secrets file the operator must download first is absent."""


def _to_node_token(creds: _Credentials) -> dict[str, dict[str, object]]:
    """Remap google-auth ``Credentials`` → nspady's Node token file.

    ``nspady/google-calendar-mcp`` (Node google-auth-library) wants
    ``access_token`` / ``refresh_token`` / ``scope`` (space-delimited) / ``token_type``
    / ``expiry_date`` (epoch ms), keyed by account id. We wrap under ``normal`` (the
    server's default account mode) so it skips its single→multi-account migration
    write. ``expiry_date`` is omitted when unknown — nspady then refreshes on first use
    via the ``refresh_token`` (the one field that actually matters for an always-on
    service; the access token is short-lived and re-minted on demand).
    """
    account: dict[str, object] = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scope": " ".join(creds.scopes or ()),
        "token_type": "Bearer",
    }
    if creds.expiry is not None:
        account["expiry_date"] = int(
            creds.expiry.replace(tzinfo=UTC).timestamp() * 1000
        )
    return {"normal": account}


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
) -> Path:
    """Run consent and write the nspady Node token to ``token_out``.

    ``consent`` is injected in tests; production uses :func:`_run_local_consent`
    (resolved late so a monkeypatch on the module global takes effect). The credentials
    are remapped to nspady's shape via :func:`_to_node_token` before writing. Raises
    :class:`ClientSecretsMissing` if the client file isn't present.
    """
    if not client_secrets.exists():
        raise ClientSecretsMissing(str(client_secrets))
    resolver = consent if consent is not None else _run_local_consent
    credentials = resolver(client_secrets, list(scopes))
    token_out.parent.mkdir(parents=True, exist_ok=True)
    token_out.write_text(
        json.dumps(_to_node_token(credentials), indent=2), encoding="utf-8"
    )
    return token_out


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: mint the token, print the next step. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chief.tools.calendar.auth",
        description="Mint a Google Calendar refresh token via local OAuth consent.",
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
        f"Wrote {out}. docker-compose bind-mounts it into the mcp-gcal container as "
        "its token store (writable — nspady persists refreshed access tokens there)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
