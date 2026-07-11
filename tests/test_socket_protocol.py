"""Wire-protocol vocabulary for the client-plane socket (#130, #131)."""

import base64

import pytest

from chief.client_plane import (
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    PROTOCOL_VERSION,
    TYPE_COMMAND,
    TYPE_FILE,
    TYPE_MILESTONE,
    TYPE_REPLY,
    TYPE_USER,
    FrameError,
    command_frame,
    decode,
    encode,
    error_frame,
    file_frame,
    hello_frame,
    milestone_frame,
    pong_frame,
    reply_frame,
    user_frame,
)


def test_hello_frame_carries_the_protocol_version() -> None:
    assert PROTOCOL_VERSION == 1
    assert hello_frame() == {"type": "hello", "protocol": 1}


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


def test_cli_frames_survive_encode_decode() -> None:
    for frame in (
        user_frame("cli:main", "hi"),
        command_frame("cli:main", "tasks", "arg"),
        reply_frame("cli:main", "answer"),
        milestone_frame("cli:main", "step"),
        file_frame("cli:main", "r.md", b"bytes", caption="c"),
    ):
        assert decode(encode(frame)) == frame
