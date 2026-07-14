"""Wire-protocol vocabulary for the client-plane socket (#130, #131, #136)."""

import base64

import pytest

from chief.client_plane import (
    CARD_OPTIONS,
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    PROTOCOL_VERSION,
    TYPE_ANSWER,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_COMMAND,
    TYPE_FILE,
    TYPE_MILESTONE,
    TYPE_REPLY,
    TYPE_USER,
    FrameError,
    answer_frame,
    backfill_frame,
    card_frame,
    card_resolved_frame,
    command_frame,
    decode,
    delta_frame,
    encode,
    error_frame,
    file_frame,
    hello_frame,
    list_threads_frame,
    milestone_frame,
    pong_frame,
    reply_frame,
    switch_frame,
    threads_frame,
    tool_frame,
    user_frame,
)
from chief.gate.approvals import ApprovalAction


def test_hello_frame_carries_the_protocol_version() -> None:
    assert PROTOCOL_VERSION == 2
    assert hello_frame() == {"type": "hello", "protocol": 2}


def test_pong_frame_shape() -> None:
    assert pong_frame() == {"type": "pong"}


def test_error_frame_shape() -> None:
    assert error_frame("invalid_json", "boom") == {
        "type": "error",
        "code": "invalid_json",
        "message": "boom",
    }


def test_encode_terminates_with_one_trailing_lf() -> None:
    wire = encode(pong_frame())
    assert wire.endswith(b"\n")
    # Exactly one LF, at the very end — a frame is one JSON object per line.
    assert wire.count(b"\n") == 1


def test_encode_decode_roundtrips() -> None:
    assert decode(encode(hello_frame())) == hello_frame()


def test_decode_unparseable_raises_invalid_json() -> None:
    with pytest.raises(FrameError) as excinfo:
        decode(b"{not json\n")
    assert excinfo.value.code == "invalid_json"


def test_decode_non_object_raises_invalid_frame() -> None:
    # Parseable JSON, but a bare scalar is not a frame object.
    with pytest.raises(FrameError) as excinfo:
        decode(b"42\n")
    assert excinfo.value.code == "invalid_frame"


def test_type_consts_match_wire_values() -> None:
    # The consts ARE the wire strings — a client and server agree on them by name.
    assert (TYPE_USER, TYPE_COMMAND) == ("user", "command")
    assert (TYPE_REPLY, TYPE_MILESTONE, TYPE_FILE) == ("reply", "milestone", "file")
    assert (TYPE_CARD, TYPE_CARD_RESOLVED, TYPE_ANSWER) == (
        "card",
        "card_resolved",
        "answer",
    )
    assert CLI_PLATFORM == "cli"
    assert DEFAULT_THREAD_KEY == "cli:main"


def test_user_frame_shape() -> None:
    assert user_frame("cli:main", "hi") == {
        "type": "user",
        "thread_key": "cli:main",
        "text": "hi",
    }


def test_command_frame_carries_name_without_slash_and_default_arg() -> None:
    assert command_frame("cli:main", "tasks") == {
        "type": "command",
        "thread_key": "cli:main",
        "name": "tasks",
        "arg": "",
    }
    assert command_frame("cli:main", "route", "coding")["arg"] == "coding"


def test_reply_frame_tags_platform_cli_and_thread() -> None:
    frame = reply_frame("cli:main", "done")
    assert frame == {
        "type": "reply",
        "platform": "cli",
        "thread_key": "cli:main",
        "text": "done",
    }


def test_milestone_frame_carries_no_prefix() -> None:
    # The type IS the semantics — the engine's "· " marker is stripped by the IO, so the
    # milestone text on the wire is clean.
    frame = milestone_frame("cli:main", "using Bash")
    assert frame["type"] == "milestone"
    assert frame["platform"] == "cli"
    assert frame["text"] == "using Bash"


def test_delta_frame_shape() -> None:
    frame = delta_frame("cli:main", "m1", "hel")
    assert frame == {
        "type": "delta",
        "platform": "cli",
        "thread_key": "cli:main",
        "message_id": "m1",
        "text": "hel",
        "done": False,
    }
    done = delta_frame("-100:5", "m1", "", done=True, platform="telegram")
    assert (done["platform"], done["text"], done["done"]) == ("telegram", "", True)


def test_tool_frame_shape() -> None:
    start = tool_frame("cli:main", "c1", "Bash", "start")
    assert start == {
        "type": "tool",
        "platform": "cli",
        "thread_key": "cli:main",
        "tool_call_id": "c1",
        "name": "Bash",
        "status": "start",
        "ok": None,
        "detail": "",
    }
    end = tool_frame("cli:main", "c1", "", "end", ok=False, detail="boom")
    assert (end["status"], end["ok"], end["detail"]) == ("end", False, "boom")


def test_file_frame_base64_roundtrips_the_raw_bytes() -> None:
    data = b"\x00\x01long reply\xff bytes"
    frame = file_frame("cli:main", "reply.md", data, caption="note")
    assert frame["type"] == "file"
    assert frame["platform"] == "cli"
    assert frame["filename"] == "reply.md"
    assert frame["caption"] == "note"
    # data is base64-ascii; a client decodes it back to the exact bytes.
    assert base64.b64decode(str(frame["data"])) == data


def test_file_frame_caption_defaults_to_none() -> None:
    assert file_frame("cli:main", "r.md", b"x")["caption"] is None


def test_card_frame_shape_and_default_platform() -> None:
    frame = card_frame("cli:main", 7, "Run: sudo rm -rf /")
    assert frame == {
        "type": "card",
        "platform": "cli",
        "thread_key": "cli:main",
        "approval_id": 7,
        "text": "Run: sudo rm -rf /",
        "options": [dict(o) for o in CARD_OPTIONS],
    }


def test_card_frame_carries_an_explicit_platform() -> None:
    frame = card_frame("-100:1", 9, "run it?", platform="telegram")
    assert frame["platform"] == "telegram"
    assert frame["approval_id"] == 9


def test_card_resolved_frame_shape() -> None:
    frame = card_resolved_frame("cli:main", 7, "✅ Approved (once) — by cli")
    assert frame == {
        "type": "card_resolved",
        "platform": "cli",
        "thread_key": "cli:main",
        "approval_id": 7,
        "text": "✅ Approved (once) — by cli",
    }
    assert card_resolved_frame("t", 1, "x", platform="discord")["platform"] == (
        "discord"
    )


def test_answer_frame_shape() -> None:
    assert answer_frame(7, "approve_once") == {
        "type": "answer",
        "approval_id": 7,
        "action": "approve_once",
    }


def test_card_options_round_trip_through_encode_decode() -> None:
    frame = card_frame("cli:main", 1, "text")
    assert decode(encode(frame)) == frame


def test_card_options_action_tokens_match_approval_action_drift_guard() -> None:
    # CARD_OPTIONS deliberately duplicates chief.gate.approvals.ApprovalAction rather
    # than importing it (protocol.py must stay importable with no sqlalchemy) — this
    # guard pins the two vocabularies together so they can't silently drift apart.
    assert {o["action"] for o in CARD_OPTIONS} == {a.value for a in ApprovalAction}
    assert len(CARD_OPTIONS) == 4


def test_cli_frames_survive_encode_decode() -> None:
    for frame in (
        user_frame("cli:main", "hi"),
        command_frame("cli:main", "tasks", "arg"),
        reply_frame("cli:main", "answer"),
        milestone_frame("cli:main", "step"),
        file_frame("cli:main", "r.md", b"bytes", caption="c"),
        card_frame("cli:main", 3, "run it?"),
        card_resolved_frame("cli:main", 3, "approved"),
        answer_frame(3, "approve_once"),
    ):
        assert decode(encode(frame)) == frame


def test_list_threads_frame_shape() -> None:
    assert list_threads_frame() == {"type": "list_threads"}


def test_switch_frame_shape() -> None:
    assert switch_frame("telegram", "-100:5") == {
        "type": "switch",
        "platform": "telegram",
        "thread_key": "-100:5",
    }


def test_threads_frame_copies_entries_so_later_mutation_is_isolated() -> None:
    entries = [{"platform": "cli", "thread_key": "cli:main"}]
    frame = threads_frame(entries)
    assert frame == {"type": "threads", "threads": entries}

    entries[0]["platform"] = "mutated"
    entries.append({"platform": "telegram", "thread_key": "-100:5"})

    assert frame["threads"] == [{"platform": "cli", "thread_key": "cli:main"}]


def test_backfill_frame_shape_and_round_trip() -> None:
    messages = [
        {"role": "chief", "kind": "reply", "text": "hi", "filename": None},
    ]
    frame = backfill_frame("telegram", "-100:5", messages)
    assert frame == {
        "type": "backfill",
        "platform": "telegram",
        "thread_key": "-100:5",
        "messages": messages,
    }
    assert decode(encode(frame)) == frame


def test_protocol_version_unchanged_by_navigation_frames() -> None:
    # #134's frames are additive — no version bump.
    assert PROTOCOL_VERSION == 2
