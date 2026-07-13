"""The in-process web UI (#153): the daemon's LAN-served owner cockpit.

An HTTP server hosted inside the chief daemon — the codebase's first inbound HTTP
surface. Server-rendered pages with htmx for interactions and SSE for streaming; no
Node toolchain. The chat surface behaves like another client-plane client: a
:class:`~chief.web.bridge.SocketBridge` connects to the daemon's own unix socket and
speaks the :mod:`chief.client_plane.protocol` frame vocabulary, so the browser, the
terminal client, and every chat platform see the same threads and stream. Settings,
files, and health use direct in-process access (they exceed the chat protocol's
scope). Owner-only password auth (:mod:`chief.web.auth`); localhost by default, LAN
by explicit toggle (``web_lan_enabled``).
"""

from .auth import SESSION_COOKIE, WebAuth
from .server import WebServer

__all__ = ["SESSION_COOKIE", "WebAuth", "WebServer"]
