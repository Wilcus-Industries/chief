"""One MCP server's connection: open it, register its tools, reconnect with
backoff when it dies, and park on a login wall.

An ``auth: oauth`` server that hits a real auth failure is parked as
``needs_auth`` until the owner asks to log in, and the next connect is that login
(see ``chief.mcpclient.oauth``) — once a stored refresh token has had one try.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from chief.mcpclient.calls import Live, call_tool
from chief.mcpclient.config import ServerConfig
from chief.mcpclient.oauth_types import LoginAttempt, NeedsAuth, find_needs_auth
from chief.mcpclient.transport import open_session

if TYPE_CHECKING:
    from chief.mcpclient.oauth import OAuthBroker

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 5.0

#: ``(server, tool listing) -> tool count`` — how a connection hands its
#: discovered tools to whoever owns the registry.
RegisterTools = Callable[[str, Any], int]


class Supervisor:
    """Keeps one server connected for as long as its task lives."""

    def __init__(
        self,
        config: ServerConfig,
        register_tools: RegisterTools,
        broker: "OAuthBroker | None" = None,
    ) -> None:
        self.config = config
        self._register_tools = register_tools
        self._broker = broker if config.auth == "oauth" else None
        self._live: Live | None = None
        #: A login wall nobody asked for gets one refresh retry before it parks
        #: the server; set from that retry until a connection comes up.
        self._retrying = False

    async def supervise(self, ready: asyncio.Future[int]) -> None:
        """Run until cancelled: connect, hold, and recover. ``ready`` resolves
        with the tool count on the first connect (0 when parked on a login) or
        fails if the server cannot start."""
        attempt: LoginAttempt | None = None
        try:
            while True:
                try:
                    await self._serve_once(ready, attempt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._honor_pending_cancel()
                    attempt, stop = await self._after_failure(exc, ready, attempt)
                    if stop:
                        return
        finally:
            self._drop()

    def _honor_pending_cancel(self) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # A cancel delivered mid-teardown can surface as an
            # ordinary exception instead of CancelledError itself —
            # e.g. a stdio transport's task group wraps a sibling's
            # BrokenResourceError around the very cancellation that
            # caused it. Honor the pending cancel rather than
            # treating this as a connection failure to retry, or the
            # retry loop spins up a fresh, uncancellable subprocess
            # (connect()'s single task.cancel() is already spent).
            raise asyncio.CancelledError() from None

    async def _after_failure(
        self, exc: Exception, ready: asyncio.Future[int], attempt: LoginAttempt | None
    ) -> tuple[LoginAttempt | None, bool]:
        """What a dead connection earns: ``(next login attempt, stop)``."""
        broker, name = self._broker, self.config.name
        if broker is not None:
            if attempt is not None and not attempt.connected:
                broker.login_failed(attempt, exc)  # the login itself broke
                return await self._await_login(ready, broker), False
            if find_needs_auth(exc):
                if self._retry_with_refresh(broker):
                    return None, False
                return await self._await_login(ready, broker), False
        logger.error(
            "mcp server %s connection failed; retrying in %ss",
            name, RECONNECT_DELAY_SECONDS, exc_info=exc,
        )
        self._drop()
        if not ready.done():
            ready.set_exception(RuntimeError(f"mcp server {name} failed to start"))
            if broker is not None:
                broker.disabled(name)
            return None, True
        if broker is not None:
            broker.disconnected(name)
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        return None, False

    def _retry_with_refresh(self, broker: "OAuthBroker") -> bool:
        """Before parking on a login wall nobody asked for: True when the stored
        refresh token gets one try. The access token may only have been turned
        away early, or never named an expiry; a refresh the server refuses meets
        the same wall and parks. The dead connection is not dropped, so a call in
        flight on it waits for the outcome and learns which."""
        name = self.config.name
        if self._retrying or not broker.force_refresh(name):
            return False
        logger.info("mcp server %s turned its login away; trying a refresh first", name)
        self._retrying = True
        broker.disconnected(name)
        return True

    async def _await_login(
        self, ready: asyncio.Future[int], broker: "OAuthBroker"
    ) -> LoginAttempt:
        """Park on the login wall; returns the ``/login`` that lifts it."""
        name = self.config.name
        notify = broker.needs_login(name)
        logger.info("mcp server %s needs login; waiting for the owner", name)
        self._drop()  # in-flight calls learn why, now that the state says so
        if not ready.done():
            ready.set_result(0)  # boot is not held up by a login
        if notify:
            await broker.send_notice(name)
        return await broker.next_login(name)

    async def _serve_once(
        self, ready: asyncio.Future[int], attempt: LoginAttempt | None
    ) -> None:
        config, broker = self.config, self._broker
        async with AsyncExitStack() as stack:
            auth = broker.provider_for(config, attempt) if broker else None
            session = await open_session(stack, config, auth)
            self._drop()  # a refresh retry's predecessor, held until now
            self._live = Live(session, asyncio.Event())
            count = self._register_tools(config.name, await session.list_tools())
            if not ready.done():
                ready.set_result(count)
            if broker is not None:
                broker.connected(config.name, count, attempt)
                self._retrying = False  # the next wall earns its own retry
            logger.info("mcp server %s up with %d tools", config.name, count)
            if broker is not None:
                await broker.logged_out(config.name)  # hold until a logout ends it
                raise NeedsAuth(config.name)
            await asyncio.Event().wait()  # hold the transport open until cancel

    def _drop(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.gone.set()

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and flatten its text content; failures are strings."""
        live = self._live
        if live is None or self._retrying or self._needs_login():
            return self._unavailable()
        text = await call_tool(live, tool_name, arguments)
        return self._unavailable() if text is None else text

    def _needs_login(self) -> bool:
        broker = self._broker
        return broker is not None and broker.state(self.config.name) == "needs_auth"

    def _unavailable(self) -> str:
        name = self.config.name
        if self._needs_login():
            return f"error: {name} needs login; call server_login"
        return f"error: mcp server '{name}' is not connected"
