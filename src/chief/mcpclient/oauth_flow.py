"""The SDK's OAuth provider as chief builds it: the redirect/callback pair it
drives, the line it draws between a refresh that was refused and one that could
not be answered, and the text and URL handling around them."""

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

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


class _TransientAwareProvider(OAuthClientProvider):
    """The SDK's provider, except that a token endpoint which could not answer
    is not taken for one that refused.

    The SDK reads every non-200 refresh response as a refusal: it drops the
    tokens it holds, the request after it goes out bare and meets a 401, and the
    flow walks into the redirect step — a login wall, and a "needs login" text
    to the owner, for one 503. A 5xx or a 429 raises here instead, before
    anything is dropped, so the connection fails like any other and is retried
    on the supervisor's backoff with the stored login intact. Any other non-200,
    a 4xx above all, is the server's answer and still reaches the redirect step.

    ``_handle_refresh_response`` is the SDK's private method (mcp 1.28); the
    struggling-endpoint tests fail loudly if an upgrade moves it."""

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        return await super()._handle_refresh_response(response)


def new_provider(
    config: ServerConfig, storage: FileTokenStorage, flow: LoginFlow,
    redirect_uri: str, ttl: float,
) -> OAuthClientProvider:
    """The SDK's OAuth provider for ``config``: a public client (PKCE, no
    secret) that registers itself, with ``flow`` as its redirect/callback."""
    assert config.url is not None
    metadata = OAuthClientMetadata.model_validate(
        {
            "client_name": "chief",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        }
    )
    provider = _TransientAwareProvider(
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
