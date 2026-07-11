"""The client plane — an always-on local control surface for chief (#128).

Whenever chief runs it binds a unix-domain socket speaking LF-delimited JSON frames
(:mod:`chief.client_plane.protocol`), independent of any chat platform: a chief with
zero platform tokens still boots with just the socket. :class:`SocketServer` owns the
listener's lifecycle and dispatches inbound frames to a pluggable handler (the #131 CLI
adapter installs one); the #132 client imports only the protocol vocabulary.
"""

from .protocol import (
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    PROTOCOL_VERSION,
    TYPE_COMMAND,
    TYPE_ERROR,
    TYPE_FILE,
    TYPE_HELLO,
    TYPE_MILESTONE,
    TYPE_PING,
    TYPE_PONG,
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
from .server import ConnectHook, FrameHandler, FrameSender, SocketServer

__all__ = [
    "CLI_PLATFORM",
    "DEFAULT_THREAD_KEY",
    "PROTOCOL_VERSION",
    "TYPE_COMMAND",
    "TYPE_ERROR",
    "TYPE_FILE",
    "TYPE_HELLO",
    "TYPE_MILESTONE",
    "TYPE_PING",
    "TYPE_PONG",
    "TYPE_REPLY",
    "TYPE_USER",
    "ConnectHook",
    "FrameError",
    "FrameHandler",
    "FrameSender",
    "SocketServer",
    "command_frame",
    "decode",
    "encode",
    "error_frame",
    "file_frame",
    "hello_frame",
    "milestone_frame",
    "pong_frame",
    "reply_frame",
    "user_frame",
]
