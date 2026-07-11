"""The client plane — an always-on local control surface for chief (#128).

Whenever chief runs it binds a unix-domain socket speaking LF-delimited JSON frames
(:mod:`chief.client_plane.protocol`), independent of any chat platform: a chief with
zero platform tokens still boots with just the socket. :class:`SocketServer` owns the
listener's lifecycle; future clients (#131+) import only the protocol vocabulary.
"""

from .protocol import (
    PROTOCOL_VERSION,
    FrameError,
    decode,
    encode,
    error_frame,
    hello_frame,
    pong_frame,
)
from .server import SocketServer

__all__ = [
    "PROTOCOL_VERSION",
    "FrameError",
    "SocketServer",
    "decode",
    "encode",
    "error_frame",
    "hello_frame",
    "pong_frame",
]
