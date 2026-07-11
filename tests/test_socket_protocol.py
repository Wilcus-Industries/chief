"""Wire-protocol vocabulary for the client-plane socket (#130): frames + framing."""

import pytest

from chief.client_plane import (
    PROTOCOL_VERSION,
    FrameError,
    decode,
    encode,
    error_frame,
    hello_frame,
    pong_frame,
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
