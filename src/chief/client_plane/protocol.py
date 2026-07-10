"""The client-plane wire protocol: LF-delimited JSON frames (#130).

The socket speaks one JSON object per line: UTF-8, LF-terminated, no embedded newlines
(``json.dumps`` escapes them). This module is the pure vocabulary — frame builders plus
:func:`encode`/:func:`decode` — kept free of any server/asyncio code so a future client
(#131+) can import it without pulling in the listener.

Frames:

- ``hello`` (server→client on connect): ``{"type": "hello", "protocol": <int>}``.
- ``ping`` (client→server): ``{"type": "ping"}`` → ``pong`` ``{"type": "pong"}``.
- ``error`` (server→client): ``{"type": "error", "code": <str>, "message": <str>}``.

Error codes: ``invalid_json`` (line was not parseable JSON), ``invalid_frame``
(parseable JSON but not an object), ``unknown_type`` (missing/unrecognized ``type``),
``line_too_long`` (the read stream limit was overrun; the connection then closes).
"""

import json
from collections.abc import Mapping
from typing import Final

#: The wire-protocol version announced in the hello frame. Bump on any incompatible
#: change to the frame vocabulary so a client can refuse a mismatch.
PROTOCOL_VERSION: Final[int] = 1


class FrameError(Exception):
    """A frame could not be decoded. ``code`` is the wire error code to send back."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def hello_frame() -> dict[str, object]:
    """The versioned greeting the server sends the instant a client connects."""
    return {"type": "hello", "protocol": PROTOCOL_VERSION}


def pong_frame() -> dict[str, object]:
    """The reply to a ``ping`` frame."""
    return {"type": "pong"}


def error_frame(code: str, message: str) -> dict[str, object]:
    """An error frame carrying a machine-readable ``code`` and a human ``message``."""
    return {"type": "error", "code": code, "message": message}


def encode(frame: Mapping[str, object]) -> bytes:
    """Serialize a frame to one UTF-8 line: compact JSON plus a trailing LF."""
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, object]:
    """Parse one wire line into a frame object, or raise :class:`FrameError`.

    Raises ``FrameError("invalid_json", ...)`` when the bytes are not valid UTF-8 JSON,
    and ``FrameError("invalid_frame", ...)`` when the JSON parses to something other
    than an object (a list, number, string, ...).
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FrameError("invalid_json", f"could not parse frame: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FrameError(
            "invalid_frame", f"frame must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed
