"""Entry point for the ``chief-cli`` console script (#137)."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from .app import ChiefCliApp
from .connection import SocketConnection


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chief-cli")
    parser.add_argument(
        "--socket",
        help="path to the client-plane socket (default: settings.socket_path)",
    )
    args = parser.parse_args(argv)

    # Lazy import: keeps chief.cli.app free of the daemon's heavy imports (engine,
    # persistence, ...) for a client that only ever needs the socket path.
    from chief.app import load_settings

    path = args.socket or load_settings().socket_path
    return asyncio.run(_run(path))


async def _run(path: str) -> int:
    conn = SocketConnection(path)
    try:
        await conn.connect()
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        print(
            f"chief is not listening on {path} — is the daemon running?",
            file=sys.stderr,
        )
        return 1
    await ChiefCliApp(conn).run_async()
    return 0
