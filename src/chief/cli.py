"""chief-cli: a terminal client for the daemon's unix socket.

A thin client only — quitting it leaves the daemon running.
"""

import argparse
import asyncio
import json
import sys


def main() -> None:
    """Entry point for the ``chief-cli`` script."""
    parser = argparse.ArgumentParser(description="Chat with the chief daemon.")
    parser.add_argument(
        "--socket", default="data/chief.sock", help="daemon socket path"
    )
    parser.add_argument("--thread", default="main", help="conversation thread name")
    args = parser.parse_args()
    try:
        asyncio.run(_repl(args.socket, args.thread))
    except (KeyboardInterrupt, EOFError):
        print()


async def _repl(socket_path: str, thread: str) -> None:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        print(f"cannot reach daemon at {socket_path} — is chief running?")
        sys.exit(1)
    loop = asyncio.get_running_loop()
    while True:
        text = await loop.run_in_executor(None, lambda: input("> "))
        if not text.strip():
            continue
        writer.write(json.dumps({"thread": thread, "text": text}).encode() + b"\n")
        await writer.drain()
        await _print_reply(reader)


async def _print_reply(reader: asyncio.StreamReader) -> None:
    """Print streamed deltas until the final frame arrives."""
    streamed = False
    while line := await reader.readline():
        frame = json.loads(line)
        if frame["type"] == "delta":
            print(frame["text"], end="", flush=True)
            streamed = True
        else:
            # Deltas already rendered the text; only print an unstreamed final.
            if not streamed:
                print(frame["text"], end="")
            print()
            return
    print("\nconnection closed by daemon")
    sys.exit(1)
