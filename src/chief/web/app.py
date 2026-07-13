"""The web UI's ASGI application (#153): routes, auth gate, server-rendered pages.

:func:`build_web_app` assembles a Starlette app from injected collaborators
(:class:`WebDeps`) — the authenticator and, as later slices wire them, the socket
bridge, file areas, settings writers, and health checks. Every page is rendered
server-side (:mod:`chief.web.render`); htmx drives the interactions and SSE the
streaming. All routes except ``/login``, ``/setup``, and ``/static`` sit behind the
session-cookie gate.
"""

from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import render
from .auth import SESSION_COOKIE, SESSION_MAX_AGE, WebAuth

#: Minimum password length enforced on /setup and the change-password form.
MIN_PASSWORD_LENGTH = 8

_STATIC_DIR = Path(__file__).parent / "static"

#: Paths reachable without a session: the two credential pages and the assets they
#: need. Everything else redirects to login (or setup, before a password exists).
_OPEN_PATHS = ("/login", "/setup")
_OPEN_PREFIXES = ("/static/",)


@dataclass
class WebDeps:
    """Everything the app serves with — injected, never constructed by routes."""

    auth: WebAuth


def _session_token(scope: Scope) -> str | None:
    """Pull the session cookie's value out of a raw ASGI scope, or ``None``."""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(value.decode("latin-1"))
            morsel = cookie.get(SESSION_COOKIE)
            if morsel is not None:
                return morsel.value
    return None


class AuthGate:
    """Pure-ASGI middleware: every non-open path requires a live session.

    Kept transport-level (no starlette middleware base class) so the gate cannot be
    bypassed by a route wiring mistake — it wraps the whole app, static mounts and
    all, and only the explicit allowlist passes through unauthenticated.
    """

    def __init__(self, app: ASGIApp, auth: WebAuth) -> None:
        self._app = app
        self._auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self._app(scope, receive, send)
            return
        if self._auth.session_valid(_session_token(scope)):
            await self._app(scope, receive, send)
            return
        target = "/login" if self._auth.password_set else "/setup"
        response = RedirectResponse(target, status_code=303)
        await response(scope, receive, send)


def _set_session_cookie(response: Response, token: str) -> None:
    # No ``secure`` flag: v1 is plain HTTP on a home LAN by design (the PRD's
    # threat model); TLS termination is reverse-proxy guidance, not code.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


async def _form_str(request: Request, field: str) -> str:
    value = (await request.form()).get(field)
    return value if isinstance(value, str) else ""


def build_web_app(deps: WebDeps) -> Starlette:
    """Assemble the ASGI app over ``deps``; pure construction, no I/O."""

    async def index(request: Request) -> Response:
        return RedirectResponse("/chat", status_code=303)

    async def login_form(request: Request) -> Response:
        if not deps.auth.password_set:
            return RedirectResponse("/setup", status_code=303)
        return HTMLResponse(render.login_page())

    async def login_submit(request: Request) -> Response:
        password = await _form_str(request, "password")
        if not deps.auth.verify(password):
            return HTMLResponse(
                render.login_page("Wrong password."), status_code=401
            )
        response = RedirectResponse("/chat", status_code=303)
        _set_session_cookie(response, deps.auth.issue_session())
        return response

    async def setup_form(request: Request) -> Response:
        if deps.auth.password_set:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(render.setup_page())

    async def setup_submit(request: Request) -> Response:
        if deps.auth.password_set:
            # First write wins; a later POST (another device, a replay) can never
            # overwrite the credential from outside a session.
            return HTMLResponse(
                render.login_page("A password is already set."), status_code=409
            )
        password = await _form_str(request, "password")
        confirm = await _form_str(request, "confirm")
        if len(password) < MIN_PASSWORD_LENGTH:
            return HTMLResponse(
                render.setup_page(
                    f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
                ),
                status_code=400,
            )
        if password != confirm:
            return HTMLResponse(
                render.setup_page("Passwords do not match."), status_code=400
            )
        deps.auth.set_password(password)
        response = RedirectResponse("/chat", status_code=303)
        _set_session_cookie(response, deps.auth.issue_session())
        return response

    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            deps.auth.revoke(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def chat_page(request: Request) -> Response:
        body = "<h1>Chat</h1><p class=\"note\">Chat plane not connected.</p>"
        return HTMLResponse(render.page("Chat", body, active="/chat"))

    routes = [
        Route("/", index),
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/setup", setup_form, methods=["GET"]),
        Route("/setup", setup_submit, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/chat", chat_page, methods=["GET"]),
        Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(AuthGate, auth=deps.auth)
    return app
