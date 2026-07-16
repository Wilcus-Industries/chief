"""The web UI's ASGI app: login, chat send, SSE stream, monitor list."""

import asyncio
import json
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from chief.adapters.base import Message
from chief.adapters.socket import HandleMessage
from chief.monitors.service import MonitorService
from chief.web.adapter import WebAdapter
from chief.web.auth import COOKIE_NAME, Auth
from chief.web.pages import CHAT_PAGE, LOGIN_PAGE


def build_web_app(
    auth: Auth,
    adapter: WebAdapter,
    handle: HandleMessage,
    monitors: MonitorService,
) -> Starlette:
    """Assemble the routes around the shared core services."""

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
            return PlainTextResponse("unauthorized", status_code=401)
        body = await request.json()
        thread = str(body.get("thread") or "main")
        message = Message(
            channel=adapter.name,
            sender="owner",
            thread_key=f"web:{thread}",
            text=str(body["text"]),
        )
        asyncio.get_running_loop().create_task(handle(message))
        return PlainTextResponse("", status_code=202)

    async def events(request: Request) -> Response:
        if not auth.is_authed(request):
            return PlainTextResponse("unauthorized", status_code=401)
        queue = adapter.listen()

        async def stream() -> AsyncIterator[str]:
            try:
                while True:
                    frame = await queue.get()
                    if frame.get("type") == "closed":
                        return
                    yield f"data: {json.dumps(frame)}\n\n"
            finally:
                adapter.drop(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def monitor_list(request: Request) -> Response:
        if not auth.is_authed(request):
            return PlainTextResponse("unauthorized", status_code=401)
        rows = await monitors.list_enabled()
        if not rows:
            return PlainTextResponse("none")
        return PlainTextResponse(
            "; ".join(f"#{r.id} {r.description}" for r in rows)
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/login", login, methods=["POST"]),
            Route("/send", send, methods=["POST"]),
            Route("/events", events),
            Route("/monitors", monitor_list),
        ]
    )
