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

Navigation frames (#134) — the active thread is client-side state, so these carry no
per-connection subscription; the server stays broadcast-to-all and a client filters:

- ``list_threads`` (client→server): ``{"type": "list_threads"}`` — asks for every
  platform's live threads.
- ``threads`` (server→client): ``{"type": "threads", "threads": [...]}`` — one entry
  per non-terminal task across every platform (``platform``, ``thread_key``, ``title``,
  ``status``).
- ``switch`` (client→server): ``{"type": "switch", "platform": <str>,
  "thread_key": <str>}`` — a stateless request for one thread's recent history; the
  server validates the thread exists and answers, it does not remember the switch.
- ``backfill`` (server→client): ``{"type": "backfill", "platform": <str>,
  "thread_key": <str>, "messages": [...]}`` — a bounded recent window from the #132
  log, marked distinct from live traffic by its own frame type (not a flag on an
  existing frame). File rows in ``messages`` carry ``filename`` but never bytes.

Status/skills frames (#138) — a point-in-time snapshot and a per-engine listing, both
request/response like ``switch``/``backfill``:

- ``status`` (client→server): ``{"type": "status"}`` — request a point-in-time
  snapshot.
- ``status_snapshot`` (server→client): ``{"type": "status_snapshot", "tasks": [...],
  "budget": [...], "schedules": [...]}`` — ``tasks`` is every non-terminal task across
  every platform (``platform``, ``thread_key``, ``title``, ``status``, ``model``);
  ``budget`` is one entry per currency (``currency``, ``spent``, ``cap``, ``mode``);
  ``schedules`` is the next few upcoming fires (``id``, ``kind``, ``spec``,
  ``action_type``, ``thread_key``, ``next_run``).
- ``skills`` (client→server): ``{"type": "skills"}`` — request the owner session's
  composed skill set.
- ``skills_list`` (server→client): ``{"type": "skills_list", "skills": [...]}``.

Approval frames (#136) — a card raised on **any** platform's thread is broadcast to
every client, not just the surface that raised it:

- ``card`` (server→client): ``{"type": "card", "platform": <str>, "thread_key": <str>,
  "approval_id": <int>, "text": <str>, "options": [...]}`` — an answerable approval
  card; ``options`` is :data:`CARD_OPTIONS`, the four buttons in card order.
- ``card_resolved`` (server→client): ``{"type": "card_resolved", "platform": <str>,
  "thread_key": <str>, "approval_id": <int>, "text": <str>}`` — the card's outcome.
- ``answer`` (client→server): ``{"type": "answer", "approval_id": <int>,
  "action": <str>}`` — a client's decision, ``action`` one of :data:`CARD_OPTIONS`'s
  action tokens.

Error codes: ``invalid_json`` (line was not parseable JSON), ``invalid_frame``
(parseable JSON but not an object), ``unknown_type`` (missing/unrecognized ``type``),
``line_too_long`` (the read stream limit was overrun; the connection then closes),
``invalid_fields`` (a client frame is missing a required field or has a wrong type),
``unknown_command`` (a command frame named a command the registry does not carry),
``already_resolved`` (an ``answer`` named an approval that is unknown or already
decided), ``unknown_thread`` (a ``switch`` named a ``(platform, thread_key)`` with no
task row), ``internal_error`` (an inbound handler raised — the connection loop
survives).
"""

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Final

#: The wire-protocol version announced in the hello frame. Bump on any incompatible
#: change to the frame vocabulary so a client can refuse a mismatch.
PROTOCOL_VERSION: Final[int] = 2

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
TYPE_CARD: Final[str] = "card"
TYPE_CARD_RESOLVED: Final[str] = "card_resolved"
TYPE_ANSWER: Final[str] = "answer"
TYPE_LIST_THREADS: Final[str] = "list_threads"
TYPE_THREADS: Final[str] = "threads"
TYPE_SWITCH: Final[str] = "switch"
TYPE_BACKFILL: Final[str] = "backfill"
TYPE_STATUS: Final[str] = "status"
TYPE_STATUS_SNAPSHOT: Final[str] = "status_snapshot"
TYPE_SKILLS: Final[str] = "skills"
TYPE_SKILLS_LIST: Final[str] = "skills_list"

#: The platform tag every CLI session frame carries. The engine filters every query by
#: ``platform``, so the CLI stack runs as its own platform alongside telegram/discord.
CLI_PLATFORM: Final[str] = "cli"

#: The thread key the #132 client opens its default (flat) session on when the owner
#: does not name a thread. Exported so client and server agree on the one default.
DEFAULT_THREAD_KEY: Final[str] = "cli:main"

#: The four approval buttons, in card order. Action values are the wire tokens of
#: chief.gate.approvals.ApprovalAction; a client answers with one of them.
CARD_OPTIONS: Final[tuple[dict[str, str], ...]] = (
    {"action": "approve_once", "label": "✅ Approve once"},
    {"action": "deny_once", "label": "❌ Deny once"},
    {"action": "always_allow", "label": "⭐ Always allow"},
    {"action": "always_deny", "label": "🚫 Always deny"},
)

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


def card_frame(
    thread_key: str, approval_id: int, text: str, *, platform: str = CLI_PLATFORM
) -> dict[str, object]:
    """A server→client approval card: preview text plus the four answerable options."""
    return {
        "type": TYPE_CARD,
        "platform": platform,
        "thread_key": thread_key,
        "approval_id": approval_id,
        "text": text,
        "options": [dict(option) for option in CARD_OPTIONS],
    }


def card_resolved_frame(
    thread_key: str, approval_id: int, text: str, *, platform: str = CLI_PLATFORM
) -> dict[str, object]:
    """A server→client card outcome — the resolution and who decided it."""
    return {
        "type": TYPE_CARD_RESOLVED,
        "platform": platform,
        "thread_key": thread_key,
        "approval_id": approval_id,
        "text": text,
    }


def answer_frame(approval_id: int, action: str) -> dict[str, object]:
    """A client→server approval decision (``action`` is a CARD_OPTIONS action token)."""
    return {"type": TYPE_ANSWER, "approval_id": approval_id, "action": action}


def list_threads_frame() -> dict[str, object]:
    """A client→server request for every platform's live threads (#134)."""
    return {"type": TYPE_LIST_THREADS}


def switch_frame(platform: str, thread_key: str) -> dict[str, object]:
    """A client→server switch onto ``(platform, thread_key)`` — asks for backfill."""
    return {"type": TYPE_SWITCH, "platform": platform, "thread_key": thread_key}


def threads_frame(threads: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """A server→client thread list: one entry per non-terminal task (#134)."""
    return {"type": TYPE_THREADS, "threads": [dict(t) for t in threads]}


def backfill_frame(
    platform: str, thread_key: str, messages: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """A server→client bounded recent-history batch for one thread (#134)."""
    return {
        "type": TYPE_BACKFILL,
        "platform": platform,
        "thread_key": thread_key,
        "messages": [dict(m) for m in messages],
    }


def status_frame() -> dict[str, object]:
    """A client→server request for a point-in-time engine/persistence snapshot."""
    return {"type": TYPE_STATUS}


def status_snapshot_frame(
    *,
    tasks: Sequence[Mapping[str, object]],
    budget: Sequence[Mapping[str, object]],
    schedules: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """A server→client status snapshot: cross-platform tasks, budget, schedules."""
    return {
        "type": TYPE_STATUS_SNAPSHOT,
        "tasks": [dict(t) for t in tasks],
        "budget": [dict(b) for b in budget],
        "schedules": [dict(s) for s in schedules],
    }


def skills_frame() -> dict[str, object]:
    """A client→server request for the owner session's composed skill set (#138)."""
    return {"type": TYPE_SKILLS}


def skills_list_frame(skills: Sequence[str]) -> dict[str, object]:
    """A server→client composed-skill listing (#138)."""
    return {"type": TYPE_SKILLS_LIST, "skills": list(skills)}


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
