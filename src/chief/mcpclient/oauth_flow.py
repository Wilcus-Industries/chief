"""The redirect/callback pair the SDK's OAuth provider drives, and the text and
URL handling around it."""

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from pydantic import AnyUrl

from chief.mcpclient.config import ServerConfig
from chief.mcpclient.oauth_types import (
    REDIRECT_HOST,
    LoginAttempt,
    LoginError,
    NeedsAuth,
    PendingFlow,
    Send,
    ServerAuth,
)
from chief.mcpclient.token_store import FileTokenStorage


class _ExpectedLoginWall(logging.Filter):
    """Keeps the SDK from logging a login wall as an ERROR with a traceback.

    The SDK logs every exception its flow raises. ``NeedsAuth`` is the normal
    way a server that wants a login announces itself, on every boot, and a
    ``LoginError`` (declined, expired, replaced) is an owner-facing outcome the
    broker already reports."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, NeedsAuth | LoginError)


logging.getLogger("mcp.client.auth.oauth2").addFilter(_ExpectedLoginWall())


class LoginFlow:
    """The redirect/callback pair the SDK drives for one connection attempt.

    With no ``attempt`` (boot and reconnect) the redirect step is where a login
    wall surfaces: it raises ``NeedsAuth`` instead of sending anyone a link.
    Given the attempt a ``/login`` created, it sends the link to that attempt's
    surface — once; a later redirect is a live session that lost its login, and
    goes back to ``NeedsAuth`` rather than quietly opening a second flow."""

    def __init__(
        self,
        record: ServerAuth,
        attempt: LoginAttempt | None,
        send: Send,
        clock: Callable[[], float],
        ttl: float,
    ) -> None:
        self._record = record
        self._attempt = attempt
        self._send = send
        self._clock = clock
        self._ttl = ttl
        self._flow: PendingFlow | None = None

    async def redirect(self, url: str) -> None:
        record, attempt, self._attempt = self._record, self._attempt, None
        if attempt is None:
            raise NeedsAuth(record.config.name)
        if record.attempt is not attempt:
            raise LoginError("replaced by a newer login request")
        flow = PendingFlow(
            record.config.name,
            dict(parse_qsl(urlsplit(url).query)).get("state", ""),
            self._clock(),
            asyncio.get_running_loop().create_future(),
            attempt.surface,
        )
        record.flow = self._flow = flow
        try:
            link = with_scopes(url, record.config.scopes)
            await self._send(attempt.surface, link_message(record.config.name, link))
        except BaseException:
            record.flow = None
            raise
        if not attempt.link.done():
            attempt.link.set_result(None)

    async def callback(self) -> tuple[str, str | None]:
        flow = self._flow
        if flow is None:
            raise LoginError("no login link was sent")
        try:
            return await asyncio.wait_for(flow.future, self._ttl)
        except TimeoutError:
            raise LoginError("the login link expired") from None
        finally:
            if self._record.flow is flow:
                self._record.flow = None


def new_provider(
    config: ServerConfig, storage: FileTokenStorage, flow: LoginFlow,
    redirect_uri: str, ttl: float,
) -> OAuthClientProvider:
    """The SDK's OAuth provider for ``config``: a public client (PKCE, no
    secret) that registers itself, with ``flow`` as its redirect/callback."""
    assert config.url is not None
    metadata = OAuthClientMetadata(
        client_name="chief",
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="none",
    )
    provider = OAuthClientProvider(
        config.url, metadata, storage, flow.redirect, flow.callback, timeout=ttl
    )
    storage.attach(provider.context)
    return provider


def with_scopes(url: str, scopes: tuple[str, ...] | None) -> str:
    """``url`` asking for the configured scopes instead of the SDK's pick.

    The SDK chooses a scope from what the server advertises and overwrites
    whatever the client asked for; config wins, so it is applied to the link."""
    if not scopes:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query)) | {"scope": " ".join(scopes)}
    return urlunsplit(parts._replace(query=urlencode(query)))


def link_message(server: str, url: str) -> str:
    return (
        f"Open this link and log in to {server.capitalize()}:\n{url}\n\n"
        "After you approve, the page will fail to load — that's expected. Copy "
        f"that page's full address (it starts with http://{REDIRECT_HOST}) and "
        "send it here. (If it loads and says you're connected, you're done.)"
    )


def callback_params(url: str) -> dict[str, str]:
    """The query of a pasted callback address, shrugging off the whitespace,
    quotes and angle brackets a chat app wraps around a link."""
    return dict(parse_qsl(urlsplit(url.strip().strip("<>\"'")).query))
