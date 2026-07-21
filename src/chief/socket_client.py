"""Thin unix-socket client for the daemon.

Shared wire encoding for the interactive ``chief-cli`` REPL and one-shot control
commands like ``chief compact``. The protocol is newline-delimited JSON:
outbound ``{"thread", "text"}``; inbound ``{"type": "delta"|"final", "text", ...}``.
"""

import asyncio
import json


def encode(thread: str, text: str) -> bytes:
    """Frame one outbound message for the daemon socket."""
    return json.dumps({"thread": thread, "text": text}).encode() + b"\n"


async def send_once(socket_path: str, thread: str, text: str) -> str:
    """Send one message, return the daemon's final reply text, and disconnect.

    Deltas (if any) are accumulated; the final frame's own text wins when set.
    Raises :class:`ConnectionError` if the daemon is unreachable or hangs up
    before replying.
    """
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        raise ConnectionError(
            f"cannot reach daemon at {socket_path} — is chief running?"
        ) from exc
    try:
        writer.write(encode(thread, text))
        await writer.drain()
        streamed: list[str] = []
        while line := await reader.readline():
            frame = json.loads(line)
            if frame.get("type") == "delta":
                streamed.append(frame.get("text", ""))
            else:
                return frame.get("text") or "".join(streamed)
        raise ConnectionError("daemon closed the connection before replying")
    finally:
        writer.close()
