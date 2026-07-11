"""The terminal client of the client plane (#137).

Contrast with :mod:`chief.adapters.cli`, the **daemon-side** CLI platform stack that
binds the socket and turns frames into engine turns. This package is the other end of
that same socket: a Textual TUI that dials ``settings.socket_path``, renders the frame
vocabulary from :mod:`chief.client_plane`, and lets the owner type messages, answer
approval cards, and run slash commands from a terminal.
"""

from .app import ChiefCliApp
from .connection import SocketConnection

__all__ = ["ChiefCliApp", "SocketConnection"]
