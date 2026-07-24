"""The web UI's ASGI app: login, chat, sessions, SSE stream, monitor list."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from chief.adapters.base import Message
from chief.adapters.socket import HandleMessage
from chief.agent.manager import SessionManager
from chief.hub import ObserverHub
from chief.monitors.service import MonitorService
from chief.persistence.store import MessageStore
from chief.web.adapter import WebAdapter
from chief.web.auth import COOKIE_NAME, Auth
from chief.web.pages import CHAT_PAGE, LOGIN_PAGE
from chief.web.script import SCRIPT
from chief.web.styles import STYLES
from chief.web.view import render_transcript, tool_call_response

# An iMessage group's thread_key is an opaque 32-char hex chat id; a 1:1 key is
# a phone/email handle. Groups have no core send path (see the send route).
_GROUP_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def build_web_app(
    auth: Auth,
    adapter: WebAdapter,
    hub: ObserverHub,
    handle: HandleMessage,
    monitors: MonitorService,
    store: MessageStore,
    palette: Callable[[], list[str]],
    manager: SessionManager,
) -> Starlette:
    """Assemble the routes around the shared core services.

    ``palette`` yields the current ``/command`` names for input completion;
    ``store`` backs the session list and per-thread transcript history;
    ``manager`` deletes buffers (row + live session) for the sidebar × control.
    """

    def unauthorized() -> Response:
        return PlainTextResponse("unauthorized", status_code=401)

    async def index(request: Request) -> Response:
        if not auth.is_authed(request):
            return HTMLResponse(LOGIN_PAGE.replace("{error}", ""))
        return HTMLResponse(CHAT_PAGE)

    async def login(request: Request) -> Response:
        form = await request.form()
        if not auth.check_password(str(form.get("password", ""))):
            return HTMLResponse(
                LOGIN_PAGE.replace(
                    "{error}", '<span class="err">wrong password</span>'
                ),
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME, auth.cookie_value(), httponly=True, samesite="strict"
        )
        return response

    async def send(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        body = await request.json()
        # The client sends the thread's real key; the channel is resolved from
        # the store (the device that owns the thread), never trusted from the
        # client. An unknown thread is a fresh web scratch buffer.
        thread_key = str(body.get("thread") or "web:main")
        channel = await store.channel(thread_key) or adapter.name
        if channel == "imessage" and _GROUP_RE.match(thread_key):
            # A group's send path is imsg (owner-directed), not the core
            # one-to-one adapter; the cockpit keeps groups view-only.
            return PlainTextResponse(
                "group threads are view-only — send with imsg", status_code=400
            )
        message = Message(
            channel=channel,
            sender="owner",
            thread_key=thread_key,
            text=str(body["text"]),
        )
        asyncio.get_running_loop().create_task(handle(message))
        return PlainTextResponse("", status_code=202)

    async def sessions(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        return JSONResponse(await store.list_sessions())

    async def history(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        thread = request.query_params.get("thread", "")
        if not thread:
            return JSONResponse([])
        return JSONResponse(render_transcript(await store.load(thread)))

    async def history_tool(request: Request) -> Response:
        """One tool call's args + result, fetched lazily off a row's click —
        never in the ``history`` payload above."""
        if not auth.is_authed(request):
            return unauthorized()
        thread = request.query_params.get("thread", "")
        call_id = request.query_params.get("call_id", "")
        if not thread or not call_id:
            return JSONResponse({"status": "compacted"})
        session = manager.peek(thread)
        busy = session is not None and session.busy
        messages = await store.load(thread)
        return JSONResponse(tool_call_response(messages, call_id, busy))

    async def delete(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        thread = str((await request.json()).get("thread", ""))
        # Only disposable web scratch buffers; never the primary or a real channel.
        if not thread.startswith("web:") or thread == "web:main":
            return PlainTextResponse("cannot delete this buffer", status_code=400)
        if not await manager.delete(thread):
            return PlainTextResponse("buffer is mid-turn", status_code=409)
        return PlainTextResponse("", status_code=200)

    async def commands(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        return JSONResponse(palette())

    async def events(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        queue = hub.listen(request.query_params.get("thread") or None)

        async def stream() -> AsyncIterator[str]:
            try:
                while True:
                    frame = await queue.get()
                    if frame.get("type") == "closed":
                        return
                    yield f"data: {json.dumps(frame)}\n\n"
            finally:
                hub.drop(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def monitor_list(request: Request) -> Response:
        if not auth.is_authed(request):
            return unauthorized()
        rows = await monitors.list_enabled()
        if not rows:
            return PlainTextResponse("none")
        return PlainTextResponse(
            "; ".join(f"#{r.id} {r.description}" for r in rows)
        )

    async def app_css(request: Request) -> Response:
        return Response(STYLES, media_type="text/css")

    async def app_js(request: Request) -> Response:
        return Response(SCRIPT, media_type="application/javascript")

    return Starlette(
        routes=[
            Route("/", index),
            Route("/login", login, methods=["POST"]),
            Route("/send", send, methods=["POST"]),
            Route("/sessions", sessions),
            Route("/delete", delete, methods=["POST"]),
            Route("/history", history),
            Route("/history/tool", history_tool),
            Route("/commands", commands),
            Route("/events", events),
            Route("/monitors", monitor_list),
            Route("/app.css", app_css),
            Route("/app.js", app_js),
        ]
    )
