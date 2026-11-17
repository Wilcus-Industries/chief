"""OAuth logins for MCP servers: who needs one, how it is asked for, how it ends.

The SDK's ``OAuthClientProvider`` does the protocol — discovery, registration,
PKCE, refresh. This broker is what chief adds around it. A server connects
*passively* on every boot and reconnect: a good token connects, a refreshable
one refreshes, and only a real auth failure — a refresh the token endpoint
refused, a token the server no longer accepts — reaches the redirect step, where
it becomes ``NeedsAuth``. That parks the server (one notice to the owner), after
one retry on a forced refresh when a refresh token is stored. A network error, a
5xx or a 429 is not one: that connection fails the ordinary way and is retried
with the stored login kept. A ``/login`` arms one *interactive* attempt that
sends the owner a link instead and waits, bounded, for the address the browser
ends up on.
"""

import time
from collections.abc import Callable
from pathlib import Path

from mcp.client.auth import OAuthClientProvider

from chief.audit import AuditLog
from chief.mcpclient import oauth_attempt as attempts
from chief.mcpclient import oauth_login as login
from chief.mcpclient.config import ServerConfig
from chief.mcpclient.oauth_flow import LoginFlow, new_provider
from chief.mcpclient.oauth_notice import Notices
from chief.mcpclient.oauth_types import (
    FLOW_TTL_SECONDS,
    AuthState,
    LoginAttempt,
    LoginResult,
    Send,
    ServerAuth,
    Surface,
)
from chief.mcpclient.token_store import FileTokenStorage


class OAuthBroker:
    """Tracks each ``auth: oauth`` server's login state and runs its logins."""

    def __init__(
        self,
        *,
        send: Send,
        audit: AuditLog,
        notice_to: Surface,
        redirect_uri: str,
        token_dir: Path,
        notified_path: Path,
        clock: Callable[[], float] = time.monotonic,
        flow_ttl: float = FLOW_TTL_SECONDS,
    ) -> None:
        self._send = send
        self._audit = audit
        self._redirect_uri = redirect_uri
        self._token_dir = token_dir
        self._clock = clock
        self._ttl = flow_ttl
        self._notices = Notices(send, notice_to, notified_path)
        self._servers: dict[str, ServerAuth] = {}

    # -- what the supervisor drives -----------------------------------------

    def register(self, config: ServerConfig) -> None:
        """Start tracking an ``auth: oauth`` server (``McpManager.connect`` does)."""
        self._servers[config.name] = ServerAuth(config)

    def provider_for(
        self, config: ServerConfig, attempt: LoginAttempt | None
    ) -> OAuthClientProvider:
        """A fresh SDK provider for one connection attempt (``attempt`` is the
        ``/login`` driving it, or None for a passive boot/reconnect)."""
        record = self._servers[config.name]
        flow = LoginFlow(record, attempt, self._send, self._clock, self._ttl)
        storage = self._storage(record)
        return new_provider(config, storage, flow, self._redirect_uri, self._ttl)

    def needs_login(self, server: str) -> bool:
        """Park ``server`` on a login; True when the owner has not been told."""
        record = self._servers[server]
        if record.state != "needs_auth":  # a failed login is already parked
            self._audit.record("mcp_auth_needed", server=server, reason="no_login")
        record.state = "needs_auth"
        return self._notices.due(server)

    async def send_notice(self, server: str) -> None:
        """Tell the owner ``server`` needs a login (best effort, once per wall)."""
        await self._notices.send(server)

    async def next_login(self, server: str) -> LoginAttempt:
        """Wait until the owner asks to log in to ``server``."""
        return await attempts.next_attempt(self._servers[server])

    def connected(self, server: str, count: int) -> None:
        """``server`` is up: clear its notice, and end the login that got it here."""
        record = self._servers[server]
        record.state = "connected"
        record.dropped.clear()
        self._notices.clear(server)
        if attempts.connected(record, count):
            self._audit.record("mcp_auth_complete", server=server, reason="connected")

    def disconnected(self, server: str) -> None:
        """A connected ``server`` dropped for a reason that is not a login wall."""
        if self._servers[server].state == "connected":
            self._servers[server].state = "connecting"

    def force_refresh(self, server: str) -> bool:
        """Have ``server``'s next connection refresh its stored access token
        instead of sending it; False when no refresh token is stored."""
        return self._storage(self._servers[server]).force_refresh()

    def disabled(self, server: str) -> None:
        """``server`` failed to start and is no longer being retried."""
        self._servers[server].state = "disabled"

    def login_failed(self, attempt: LoginAttempt, exc: BaseException) -> None:
        """End ``attempt`` with the reason; the server stays parked."""
        record = self._servers[attempt.server]
        reason = attempts.failed(record, attempt, exc)
        self._audit.record("mcp_auth_fail", server=attempt.server, reason=reason)

    async def logged_out(self, server: str) -> None:
        """Wait for ``logout`` to ask the live connection to end."""
        record = self._servers[server]
        await record.dropped.wait()
        record.dropped.clear()

    # -- what the owner drives ----------------------------------------------

    def state(self, server: str) -> AuthState:
        """``server``'s login state; ``disabled`` for one this broker does not run."""
        record = self._servers.get(server)
        return record.state if record else "disabled"

    def needs_auth(self) -> list[str]:
        """The servers waiting on a login, sorted."""
        return sorted(n for n, r in self._servers.items() if r.state == "needs_auth")

    async def start_login(self, server: str, surface: Surface) -> str:
        """Send ``surface`` a login link for ``server``; replaces any open one."""
        record = self._servers.get(server)
        if record is None:
            return _unknown(server)
        if record.state == "needs_auth":
            self._audit.record("mcp_auth_start", server=server, reason="requested")
        return await login.start(record, surface)

    async def complete_login(
        self, callback_url: str, server: str | None = None
    ) -> LoginResult:
        """Finish a login from the address the owner's browser ended up on."""
        records = self._servers.values()
        clock, ttl = self._clock, self._ttl
        return await login.complete(records, callback_url, server, clock, ttl)

    async def logout(self, server: str) -> str:
        """Forget ``server``'s login; the owner is not told it needs one again."""
        record = self._servers.get(server)
        if record is None:
            return _unknown(server)
        result = await login.logout(record, self._storage(record))
        if result.ok:
            self._notices.mark(server)
            self._audit.record("mcp_auth_logout", server=server, reason="requested")
        return result.message

    def _storage(self, record: ServerAuth) -> FileTokenStorage:
        """A token store that stops writing once a logout bumps the generation
        it was built in."""
        config, generation = record.config, record.generation
        assert config.url is not None
        return FileTokenStorage(
            self._token_dir, config.name, config.url, self._redirect_uri,
            writable=lambda: record.generation == generation,
        )


def _unknown(server: str) -> str:
    return f"error: '{server}' is not an OAuth MCP server"
