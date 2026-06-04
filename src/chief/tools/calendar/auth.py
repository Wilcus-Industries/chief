"""One-time Google Calendar OAuth consent → a mountable refresh token.

The only interactive piece of M5: the operator runs this once on their dev machine
(``python -m chief.tools.calendar.auth``), completes the loopback consent in a browser,
and gets an **authorized-user JSON** written to
``./secrets/google_calendar_token.json``. That file is mounted into the ``mcp-gcal``
container as the ``google_calendar_token`` Docker secret — so the VPS never runs a
consent callback server (DESIGN: "run the consent dance once locally, mount the
resulting refresh token as a Docker secret — no callback server on the VPS").

``google-auth-oauthlib`` is a **dev/host-only** dependency (imported lazily): the
runtime container reaches Google through the MCP server, not through this helper, so the
core image stays lean.
"""

import argparse
from collections.abc import Callable, Sequence
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

    def to_json(self) -> str: ...


#: Run the consent dance for ``client_secrets`` over ``scopes`` → fresh credentials.
ConsentFn = Callable[[Path, list[str]], _Credentials]


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
) -> Path:
    """Run consent and write the authorized-user token to ``token_out``.

    ``consent`` is injected in tests; production uses :func:`_run_local_consent`
    (resolved late so a monkeypatch on the module global takes effect). Raises
    :class:`ClientSecretsMissing` if the client file isn't present.
    """
    if not client_secrets.exists():
        raise ClientSecretsMissing(str(client_secrets))
    resolver = consent if consent is not None else _run_local_consent
    credentials = resolver(client_secrets, list(scopes))
    token_out.parent.mkdir(parents=True, exist_ok=True)
    token_out.write_text(credentials.to_json(), encoding="utf-8")
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
        f"Wrote {out}. Mount it into the mcp-gcal container as the Docker secret "
        "'google_calendar_token'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
