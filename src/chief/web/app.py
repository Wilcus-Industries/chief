"""The web UI's ASGI application (#153): routes, auth gate, server-rendered pages.

:func:`build_web_app` assembles a Starlette app from injected collaborators
(:class:`WebDeps`) — the authenticator and, as later slices wire them, the socket
bridge, file areas, settings writers, and health checks. Every page is rendered
server-side (:mod:`chief.web.render`); htmx drives the interactions and SSE the
streaming. All routes except ``/login``, ``/setup``, and ``/static`` sit behind the
session-cookie gate.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from ..client_plane import (
    CARD_OPTIONS,
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_ERROR,
    TYPE_FILE,
    TYPE_MILESTONE,
    TYPE_REPLY,
)
from . import render
from .auth import SESSION_COOKIE, SESSION_MAX_AGE, WebAuth
from .bridge import BridgeError, SocketBridge

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
    #: The client-plane attachment. ``None`` only when the socket is unreachable —
    #: the chat surface then renders a "not connected" note instead of crashing.
    bridge: SocketBridge | None = None


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


def _active_thread(request: Request) -> tuple[str, str]:
    """The (platform, thread_key) the page/stream is focused on — client state."""
    platform = request.query_params.get("platform") or CLI_PLATFORM
    thread_key = request.query_params.get("thread_key") or DEFAULT_THREAD_KEY
    return platform, thread_key


def _sse_format(event: str, data: str) -> str:
    """One SSE event: named, with multi-line data split across ``data:`` lines."""
    payload = "".join(f"data: {line}\n" for line in (data.splitlines() or [""]))
    return f"event: {event}\n{payload}\n"


def _sse_events_for(
    frame: dict[str, object],
    *,
    platform: str,
    thread_key: str,
    bridge: SocketBridge,
) -> list[tuple[str, str]]:
    """Translate one broadcast frame into named SSE events for one open page.

    The active thread is client state (#134): replies/milestones/files render only
    when they match it, while approval traffic re-renders the approvals block for
    EVERY page — a card raised on any platform is answerable from any browser tab.
    """
    ftype = frame.get("type")
    matches = (
        frame.get("platform") == platform and frame.get("thread_key") == thread_key
    )
    events: list[tuple[str, str]] = []
    if ftype == TYPE_REPLY and matches:
        events.append(
            ("message", render.message_html("reply", str(frame.get("text"))))
        )
    elif ftype == TYPE_MILESTONE and matches:
        events.append(
            ("message", render.message_html("milestone", str(frame.get("text"))))
        )
    elif ftype == TYPE_FILE and matches:
        caption = frame.get("caption")
        events.append(
            (
                "message",
                render.file_message_html(
                    platform,
                    thread_key,
                    str(frame.get("filename")),
                    str(caption) if caption is not None else None,
                ),
            )
        )
    elif ftype in (TYPE_CARD, TYPE_CARD_RESOLVED):
        events.append(("approvals", render.approvals_html(bridge.pending_cards())))
        if ftype == TYPE_CARD_RESOLVED and matches:
            events.append(
                ("message", render.message_html("milestone", str(frame.get("text"))))
            )
    elif ftype == TYPE_ERROR:
        # Connection-scoped (a failed command, a refused answer) — single-owner
        # surface, so a dim transcript notice on every open page is honest enough.
        events.append(
            ("message", render.message_html("milestone", f"⚠ {frame.get('message')}"))
        )
    return events


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
        if deps.bridge is None:
            body = '<h1>Chat</h1><p class="note">Chat plane not connected.</p>'
            return HTMLResponse(render.page("Chat", body, active="/chat"))
        platform, thread_key = _active_thread(request)
        threads = await deps.bridge.threads()
        try:
            history = await deps.bridge.backfill(platform, thread_key)
        except BridgeError:
            # A thread with no task row yet (day one, or a freshly minted key)
            # simply has no history — the composer still dispatches into it.
            history = []
        body = render.chat_page_body(
            platform=platform,
            thread_key=thread_key,
            threads=threads,
            history=history,
            cards=deps.bridge.pending_cards(),
        )
        return HTMLResponse(render.page("Chat", body, active="/chat"))

    async def chat_threads(request: Request) -> Response:
        if deps.bridge is None:
            return HTMLResponse("")
        platform, thread_key = _active_thread(request)
        return HTMLResponse(
            render.thread_list_html(
                await deps.bridge.threads(),
                active_platform=platform,
                active_thread=thread_key,
            )
        )

    async def chat_send(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        form = await request.form()
        platform = str(form.get("platform") or CLI_PLATFORM)
        thread_key = str(form.get("thread_key") or DEFAULT_THREAD_KEY)
        text = form.get("text")
        if not isinstance(text, str) or not text.strip():
            return PlainTextResponse("text is required", status_code=400)
        text = text.strip()
        if platform == CLI_PLATFORM and text.startswith("/"):
            name, _, arg = text[1:].partition(" ")
            await deps.bridge.send_command(thread_key, name, arg.strip())
        elif platform == CLI_PLATFORM:
            await deps.bridge.send_user(thread_key, text)
        else:
            # A foreign platform's thread: a real owner turn on that platform's own
            # engine (#135) — echoed onto the phone chat, replied via its mirror.
            await deps.bridge.send_inject(platform, thread_key, text)
        # The socket does not echo inbound frames back, so the POST response IS the
        # owner line — htmx appends it to the transcript.
        return HTMLResponse(render.message_html("user", text, role="owner"))

    async def chat_cancel(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        form = await request.form()
        platform = str(form.get("platform") or CLI_PLATFORM)
        thread_key = str(form.get("thread_key") or DEFAULT_THREAD_KEY)
        if platform != CLI_PLATFORM:
            return PlainTextResponse(
                "cancel is only available on chief's own threads", status_code=400
            )
        await deps.bridge.send_command(thread_key, "cancel")
        return PlainTextResponse("")

    async def chat_new(request: Request) -> Response:
        # Mint a fresh client-side thread key; the task row appears on first send.
        thread_key = f"cli:w{time.time_ns()}"
        return RedirectResponse(
            f"/chat?platform={CLI_PLATFORM}&thread_key={thread_key}",
            status_code=303,
        )

    async def approvals_partial(request: Request) -> Response:
        if deps.bridge is None:
            return HTMLResponse("")
        return HTMLResponse(render.approvals_html(deps.bridge.pending_cards()))

    async def approvals_answer(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        approval_id = int(request.path_params["approval_id"])
        action = await _form_str(request, "action")
        if action not in {option["action"] for option in CARD_OPTIONS}:
            return PlainTextResponse(f"unknown action {action!r}", status_code=400)
        if approval_id not in deps.bridge.cards:
            # The bridge's card ledger is authoritative: a resolved (or never-seen)
            # id means someone already decided — first answer wins, everywhere.
            return PlainTextResponse(
                f"approval {approval_id} is unknown or already resolved",
                status_code=409,
            )
        await deps.bridge.answer(approval_id, action)
        # Optimistic render: drop the just-answered card now; the authoritative
        # card_resolved broadcast re-renders the block for every other page.
        remaining = [
            card
            for card in deps.bridge.pending_cards()
            if card.get("approval_id") != approval_id
        ]
        return HTMLResponse(render.approvals_html(remaining))

    async def events(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        bridge = deps.bridge
        platform, thread_key = _active_thread(request)
        subscription = bridge.subscribe()

        async def stream() -> AsyncIterator[str]:
            with subscription:
                yield "retry: 2000\n\n"
                while True:
                    frame = await subscription.queue.get()
                    for name, fragment in _sse_events_for(
                        frame,
                        platform=platform,
                        thread_key=thread_key,
                        bridge=bridge,
                    ):
                        yield _sse_format(name, fragment)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/", index),
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/setup", setup_form, methods=["GET"]),
        Route("/setup", setup_submit, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/chat", chat_page, methods=["GET"]),
        Route("/chat/threads", chat_threads, methods=["GET"]),
        Route("/chat/send", chat_send, methods=["POST"]),
        Route("/chat/cancel", chat_cancel, methods=["POST"]),
        Route("/chat/new", chat_new, methods=["GET"]),
        Route("/approvals", approvals_partial, methods=["GET"]),
        Route("/approvals/{approval_id:int}", approvals_answer, methods=["POST"]),
        Route("/events", events, methods=["GET"]),
        Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(AuthGate, auth=deps.auth)
    return app
