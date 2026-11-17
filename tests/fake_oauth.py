"""A local OAuth 2.1 authorization server + MCP resource server, for tests.

One uvicorn app on an ephemeral loopback port serves the protected-resource
metadata, the authorization-server metadata, dynamic client registration, the
authorize / token / revoke endpoints (PKCE S256 verified, refresh tokens, knobs
that make either grant fail), and a FastMCP streamable-HTTP endpoint at ``/mcp``
behind a bearer gate (or, with ``oauth_enabled=False``, a static-header gate).
Nothing here talks to a real service.
"""

import asyncio
import base64
import hashlib
import secrets
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


def _oauth_error(error: str, status: int = 400) -> Response:
    return JSONResponse({"error": error}, status_code=status)


class FakeOAuth:
    """State, knobs and observations for one fake authorization/resource server."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.mcp_url = f"{base_url}/mcp"
        # Knobs: flip these from a test to steer the next request.
        self.oauth_enabled = True  # False: /mcp needs no bearer (static headers)
        self.required_headers: dict[str, str] = {}  # lower-cased name -> value
        self.token_ttl = 3600
        self.expires_in_sent = True  # False: token responses name no lifetime
        self.refresh_error: str | None = None  # e.g. "invalid_grant"
        # Answer a refresh with this bare status instead, no OAuth error body: 503
        # or 429 is an endpoint having a bad moment, 401 one refusing the client.
        self.refresh_status: int | None = None
        self.refresh_delay = 0.0  # seconds a refresh grant sits before it is answered
        self.exchange_error: str | None = None  # fail the authorization_code grant
        self.rotate_refresh = True  # False: refresh responses omit refresh_token
        self.revocation = False  # advertise a revocation endpoint
        self.revoke_status = 200  # what the revocation endpoint answers
        self.deny_authorize = False  # the browser step answers access_denied
        self.as_base: str | None = None  # where PRM says the AS lives (default: here)
        # Where the AS metadata puts the token endpoint. Never "/token": that is
        # where the SDK guesses when it has no metadata, so serving it there would
        # let a client that forgot the metadata pass. Fixed when the app is built,
        # so pass it to ``serve_fake``.
        self.token_path = "/oauth2/token/"
        # Observations.
        self.clients: dict[str, dict[str, Any]] = {}
        self.registrations: list[dict[str, Any]] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.revocations: list[dict[str, str]] = []
        self.seen_headers: list[dict[str, str]] = []
        self.requests: list[tuple[str, str]] = []  # (method, path), every one
        self._codes: dict[str, dict[str, str]] = {}
        self._access: dict[str, float] = {}
        self._refresh: set[str] = set()

    # -- gate ---------------------------------------------------------------

    def authorized(self, headers: dict[str, str]) -> bool:
        if any(headers.get(k) != v for k, v in self.required_headers.items()):
            return False
        if not self.oauth_enabled:
            return True
        token = headers.get("authorization", "").removeprefix("Bearer ")
        return self._access.get(token, 0.0) > time.time()

    def unauthorized(self) -> Response:
        if not self.oauth_enabled:
            return Response(status_code=401)
        challenge = (
            f'Bearer resource_metadata="{self.base_url}'
            '/.well-known/oauth-protected-resource/mcp"'
        )
        return Response(status_code=401, headers={"WWW-Authenticate": challenge})

    def revoke_access_tokens(self) -> None:
        """Server-side revocation: every issued access token stops working."""
        self._access.clear()

    # -- the browser --------------------------------------------------------

    async def browser_approve(self, authorization_url: str) -> str:
        """Act as the owner's browser: open the link, return where it redirects
        (the ``redirect_uri?code=...&state=...`` the owner would paste back)."""
        async with httpx.AsyncClient() as client:
            response = await client.get(authorization_url)
        assert response.status_code == 302, response.text
        return response.headers["location"]

    # -- endpoints ----------------------------------------------------------

    async def _prm(self, _request: Request) -> Response:
        return JSONResponse(
            {
                "resource": self.mcp_url,
                "authorization_servers": [self.as_base or self.base_url],
                "scopes_supported": ["internal"],
            }
        )

    async def _as_metadata(self, _request: Request) -> Response:
        metadata: dict[str, Any] = {
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}{self.token_path}",
            "registration_endpoint": f"{self.base_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["internal"],
        }
        if self.revocation:
            metadata["revocation_endpoint"] = f"{self.base_url}/revoke"
        return JSONResponse(metadata)

    async def _register(self, request: Request) -> Response:
        body = await request.json()
        self.registrations.append(body)
        client_id = f"client-{len(self.clients) + 1}"
        self.clients[client_id] = {**body, "client_id": client_id}
        return JSONResponse(self.clients[client_id], status_code=201)

    async def _authorize(self, request: Request) -> Response:
        query = dict(request.query_params)
        client = self.clients.get(query.get("client_id", ""))
        redirect = query.get("redirect_uri", "")
        if client is None or redirect not in client["redirect_uris"]:
            return _oauth_error("invalid_request")
        if query.get("code_challenge_method") != "S256":
            return _oauth_error("invalid_request")
        self.authorize_requests.append(query)
        if self.deny_authorize:
            params = {"error": "access_denied", "state": query["state"]}
        else:
            code = secrets.token_urlsafe(16)
            self._codes[code] = {
                "client_id": query["client_id"],
                "redirect_uri": redirect,
                "challenge": query["code_challenge"],
            }
            params = {"code": code, "state": query["state"]}
        location = f"{redirect}?{httpx.QueryParams(params)}"
        return Response(status_code=302, headers={"Location": location})

    async def _token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.token_requests.append(form)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self._exchange(form)
        if grant == "refresh_token":
            await asyncio.sleep(self.refresh_delay)
            return self._refresh_grant(form)
        return _oauth_error("unsupported_grant_type")

    def _exchange(self, form: dict[str, str]) -> Response:
        if self.exchange_error:
            return _oauth_error(self.exchange_error)
        grant = self._codes.pop(form.get("code", ""), None)  # single use
        if grant is None:
            return _oauth_error("invalid_grant")
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        same = (
            grant["client_id"] == form.get("client_id")
            and grant["redirect_uri"] == form.get("redirect_uri")
            and grant["challenge"] == challenge
        )
        return self._issue() if same else _oauth_error("invalid_grant")

    def _refresh_grant(self, form: dict[str, str]) -> Response:
        if self.refresh_status:  # before anything is spent: the token stays good
            return Response(status_code=self.refresh_status)
        if self.refresh_error:
            return _oauth_error(self.refresh_error)
        token = form.get("refresh_token", "")
        if token not in self._refresh:
            return _oauth_error("invalid_grant")
        if self.rotate_refresh:
            self._refresh.discard(token)
            return self._issue()
        return self._issue(with_refresh=False)

    def _issue(self, *, with_refresh: bool = True) -> Response:
        access = secrets.token_urlsafe(24)
        self._access[access] = time.time() + self.token_ttl
        body: dict[str, Any] = {"access_token": access, "token_type": "Bearer"}
        if self.expires_in_sent:
            body["expires_in"] = self.token_ttl
        if with_refresh:
            refresh = secrets.token_urlsafe(24)
            self._refresh.add(refresh)
            body["refresh_token"] = refresh
        return JSONResponse(body)

    async def _revoke(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        self.revocations.append(form)
        self._refresh.discard(form.get("token", ""))
        self._access.pop(form.get("token", ""), None)
        return Response(status_code=self.revoke_status)

    # -- assembly -----------------------------------------------------------

    def build_app(self) -> ASGIApp:
        mcp = FastMCP("fake-resource")

        @mcp.tool()
        def ping() -> str:
            """Reply pong."""
            return "pong"

        app: Starlette = mcp.streamable_http_app()
        for path, handler, method in (
            ("/.well-known/oauth-protected-resource/mcp", self._prm, "GET"),
            ("/.well-known/oauth-authorization-server", self._as_metadata, "GET"),
            ("/register", self._register, "POST"),
            ("/authorize", self._authorize, "GET"),
            (self.token_path, self._token, "POST"),
            ("/revoke", self._revoke, "POST"),
        ):
            app.add_route(path, handler, methods=[method])
        return _Gate(app, self)


class _Gate:
    """ASGI wrapper that logs every request and gates the ones to ``/mcp``."""

    def __init__(self, app: ASGIApp, fake: FakeOAuth) -> None:
        self._app = app
        self._fake = fake

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self._fake.requests.append((scope["method"], scope["path"]))
        if scope["type"] == "http" and scope["path"] == "/mcp":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self._fake.seen_headers.append(headers)
            if not self._fake.authorized(headers):
                await self._fake.unauthorized()(scope, receive, send)
                return
        await self._app(scope, receive, send)


@asynccontextmanager
async def serve_fake(**knobs: Any) -> AsyncIterator[FakeOAuth]:
    """Run a ``FakeOAuth`` on an ephemeral loopback port for the ``async with``.

    ``knobs`` are set as attributes on the fake before it starts serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    fake = FakeOAuth(f"http://127.0.0.1:{sock.getsockname()[1]}")
    for name, value in knobs.items():
        setattr(fake, name, value)
    config = uvicorn.Config(
        fake.build_app(), log_level="warning", lifespan="on",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(0.01)
    try:
        yield fake
    finally:
        server.should_exit = True
        try:
            await task
        finally:
            sock.close()
            # sse_starlette latches this process-wide flag when its watcher (it
            # polls every half second) sees a server shutting down, and after
            # that every SSE response in the process ends at once. A teardown
            # slowed by a request still in flight would break the next test's
            # server, so it is cleared here.
            AppStatus.should_exit = False
