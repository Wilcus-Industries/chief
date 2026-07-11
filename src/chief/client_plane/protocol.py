"""The client-plane wire protocol: LF-delimited JSON frames (#130, #131).

The socket speaks one JSON object per line: UTF-8, LF-terminated, no embedded newlines
(``json.dumps`` escapes them). This module is the pure vocabulary — frame builders plus
:func:`encode`/:func:`decode` — kept free of any server/asyncio code so a client (the
#132 CLI) can import it without pulling in the listener.

Transport frames (#130):

- ``hello`` (server→client on connect): ``{"type": "hello", "protocol": <int>}``.
- ``ping`` (client→server): ``{"type": "ping"}`` → ``pong`` ``{"type": "pong"}``.
- ``error`` (server→client): ``{"type": "error", "code": <str>, "message": <str>}``.

Session frames are tagged with their **originating platform** (``cli``, ``telegram``,
``discord``) + ``thread_key`` on every server→client frame so a client can demux threads
(broadcast-to-all; clients filter). #131 introduced them for the CLI stack; #133 mirrors
every stack's engine outbound onto the socket, so a frame's ``platform`` names the stack
it came from rather than always ``cli``:

- ``user`` (client→server): ``{"type": "user", "thread_key": <str>, "text": <str>}``.
- ``command`` (client→server): ``{"type": "command", "thread_key": <str>,
  "name": <str>, "arg": <str>}`` — ``name`` carries no leading ``/``.
- ``reply`` (server→client): a final answer block for a thread.
- ``milestone`` (server→client): a progress line (the engine's ``· `` prefix is
  stripped — the frame *type* carries that semantics, not a text marker).
- ``file`` (server→client): an oversized reply delivered as a base64 attachment. The
  ``data`` field is base64-ascii, so a client must decode it; a client's
  ``open_unix_connection`` read limit must be large enough for the encoded frame.

A server→client frame re-delivered from the #132 message log after a detached period
carries ``"replay": true``; live frames omit the field. It is additive (the field is
just added to the stored frame on re-emit), so it does **not** bump ``PROTOCOL_VERSION``
— an older client that ignores the key still renders the frame correctly.

Error codes: ``invalid_json`` (line was not parseable JSON), ``invalid_frame``
(parseable JSON but not an object), ``unknown_type`` (missing/unrecognized ``type``),
``line_too_long`` (the read stream limit was overrun; the connection then closes),
``invalid_fields`` (a client frame is missing a required field or has a wrong type),
``unknown_command`` (a command frame named a command the registry does not carry),
``internal_error`` (an inbound handler raised — the connection loop survives).
"""

import base64
import json
from collections.abc import Mapping
from typing import Final

#: The wire-protocol version announced in the hello frame. Bump on any incompatible
#: change to the frame vocabulary so a client can refuse a mismatch.
PROTOCOL_VERSION: Final[int] = 1

#: Frame ``type`` strings. Plain ``Final[str]`` consts, NOT an enum — frames stay plain
#: dicts and :func:`decode` stays type-agnostic (it never validates the type). One name
#: per wire value so a typo is a NameError, not a silently-wrong frame.
TYPE_HELLO: Final[str] = "hello"
TYPE_PING: Final[str] = "ping"
TYPE_PONG: Final[str] = "pong"
TYPE_ERROR: Final[str] = "error"
TYPE_USER: Final[str] = "user"
TYPE_COMMAND: Final[str] = "command"
TYPE_REPLY: Final[str] = "reply"
TYPE_MILESTONE: Final[str] = "milestone"
TYPE_FILE: Final[str] = "file"

#: The platform tag every CLI session frame carries. The engine filters every query by
#: ``platform``, so the CLI stack runs as its own platform alongside telegram/discord.
CLI_PLATFORM: Final[str] = "cli"

#: The thread key the #132 client opens its default (flat) session on when the owner
#: does not name a thread. Exported so client and server agree on the one default.
DEFAULT_THREAD_KEY: Final[str] = "cli:main"

#: The milestone marker the engine prefixes onto a progress line before it reaches the
#: ``TaskIO.send`` seam (``core.tasks._run_turn``, test-pinned at tasks.py:1729). The
#: engine streams milestones and final replies through the same ``send`` call,
#: distinguished only by this prefix; :func:`outbound_frame` splits them back into the
#: two frame types so every stack — CLI and the #133 mirror — shares one split.
MILESTONE_PREFIX: Final[str] = "· "


class FrameError(Exception):
    """A frame could not be decoded. ``code`` is the wire error code to send back."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def hello_frame() -> dict[str, object]:
    """The versioned greeting the server sends the instant a client connects."""
    return {"type": TYPE_HELLO, "protocol": PROTOCOL_VERSION}


def pong_frame() -> dict[str, object]:
    """The reply to a ``ping`` frame."""
    return {"type": TYPE_PONG}


def error_frame(code: str, message: str) -> dict[str, object]:
    """An error frame carrying a machine-readable ``code`` and a human ``message``."""
    return {"type": TYPE_ERROR, "code": code, "message": message}


def user_frame(thread_key: str, text: str) -> dict[str, object]:
    """A client→server owner message for ``thread_key`` (#131)."""
    return {"type": TYPE_USER, "thread_key": thread_key, "text": text}


def command_frame(thread_key: str, name: str, arg: str = "") -> dict[str, object]:
    """A client→server owner slash-command (``name`` carries no leading ``/``, #131)."""
    return {"type": TYPE_COMMAND, "thread_key": thread_key, "name": name, "arg": arg}


def reply_frame(
    thread_key: str, text: str, *, platform: str = CLI_PLATFORM
) -> dict[str, object]:
    """A server→client final answer block, tagged ``platform`` + thread (#131, #133)."""
    return {
        "type": TYPE_REPLY,
        "platform": platform,
        "thread_key": thread_key,
        "text": text,
    }


def milestone_frame(
    thread_key: str, text: str, *, platform: str = CLI_PLATFORM
) -> dict[str, object]:
    """A server→client progress line (no ``· `` prefix — the type is the marker)."""
    return {
        "type": TYPE_MILESTONE,
        "platform": platform,
        "thread_key": thread_key,
        "text": text,
    }


def file_frame(
    thread_key: str,
    filename: str,
    data: bytes,
    caption: str | None = None,
    *,
    platform: str = CLI_PLATFORM,
) -> dict[str, object]:
    """A server→client attachment: ``data`` bytes base64-ascii encoded (#131).

    An oversized owner reply (over the file threshold, or an un-splittable code fence)
    is delivered as one file frame rather than a wall of hard-cut messages. The client
    base64-decodes ``data`` back to bytes; ``caption`` is the short note beside it.
    """
    return {
        "type": TYPE_FILE,
        "platform": platform,
        "thread_key": thread_key,
        "filename": filename,
        "data": base64.b64encode(data).decode("ascii"),
        "caption": caption,
    }


def outbound_frame(
    thread_key: str, text: str, *, platform: str = CLI_PLATFORM
) -> dict[str, object]:
    """Split one engine ``send`` into a milestone or reply frame by its ``· `` prefix.

    The engine streams milestones through the same ``TaskIO.send`` seam as final
    replies, distinguished only by :data:`MILESTONE_PREFIX`; this recovers the two frame
    types so a client can render progress and answers differently. Shared by the CLI IO
    and the #133 mirror so the split lives in exactly one place.
    """
    if text.startswith(MILESTONE_PREFIX):
        body = text.removeprefix(MILESTONE_PREFIX)
        return milestone_frame(thread_key, body, platform=platform)
    return reply_frame(thread_key, text, platform=platform)


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
