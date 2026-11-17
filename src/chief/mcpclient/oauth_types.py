"""What the OAuth pieces pass around: surfaces, per-server state, the open
login's records, and the exceptions that carry a login's fate."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from chief.mcpclient.config import ServerConfig

#: (channel, thread_key): the conversation a login was asked for in.
Surface = tuple[str, str]
Send = Callable[[Surface, str], Awaitable[None]]
AuthState = Literal["connecting", "connected", "needs_auth", "disabled"]

#: The loopback host in the registered redirect URI. One constant so switching
#: to ``localhost`` (should a provider insist) is a one-line change; a stored
#: registration made under the old host is ignored, see ``FileTokenStorage``.
REDIRECT_HOST = "127.0.0.1"
CALLBACK_PATH = "/oauth/callback"
FLOW_TTL_SECONDS = 600.0


class NeedsAuth(Exception):
    """A server wants a login nobody has asked for.

    Raised from the redirect step of an attempt that is not a ``/login``: the
    SDK reaches that step only after a real auth failure (no token, a refresh
    that failed, a token the server no longer accepts), never for a network
    error or a 5xx, so catching it is the whole "needs login" test."""

    def __init__(self, server: str) -> None:
        super().__init__(f"mcp server {server} needs login")
        self.server = server


class LoginError(Exception):
    """A login attempt that cannot finish; the message is for the owner."""


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    message: str


@dataclass
class PendingFlow:
    """An authorization link the owner has been sent and not yet answered.

    ``future`` is what the SDK's callback step awaits: resolved with
    ``(code, state)`` when the owner's address arrives, or failed with the
    reason the link is dead."""

    server: str
    state: str
    created_at: float
    future: asyncio.Future[tuple[str, str | None]]


@dataclass
class LoginAttempt:
    """One ``/login``: where to answer, whether the link went out (``link``)
    and how it ended (``done``)."""

    server: str
    surface: Surface
    link: asyncio.Future[None]
    done: asyncio.Future[LoginResult]
    connected: bool = False

    def finish(self, result: LoginResult) -> None:
        if not self.done.done():
            self.done.set_result(result)


@dataclass
class ServerAuth:
    """What the broker knows about one ``auth: oauth`` server."""

    config: ServerConfig
    state: AuthState = "connecting"
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # a login was asked for
    dropped: asyncio.Event = field(default_factory=asyncio.Event)  # logout
    attempt: LoginAttempt | None = None
    flow: PendingFlow | None = None

    def reject_flow(self, reason: str) -> None:
        """End the open link: whoever waits on its answer gets ``reason``."""
        flow, self.flow = self.flow, None
        if flow is not None and not flow.future.done():
            flow.future.set_exception(LoginError(reason))


def leaf(exc: BaseException) -> BaseException:
    """The first real error inside (possibly nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def find_needs_auth(exc: BaseException) -> bool:
    """Whether ``NeedsAuth`` is ``exc`` or anywhere inside its groups — the
    SDK's task groups wrap what a request raised."""
    if isinstance(exc, BaseExceptionGroup):
        return any(find_needs_auth(inner) for inner in exc.exceptions)
    return isinstance(exc, NeedsAuth)
