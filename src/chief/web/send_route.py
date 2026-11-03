"""The dashboard's ``/send`` route: queue a turn, or interrupt a running one.

Split out of ``app.py`` to keep that file under the length cap. The reply body
names what became of the message, so the client knows when its pending bubble
resolves: ``queued`` and ``injected`` both stream an ``inbound`` frame once the
agent actually takes the text; ``answered`` (it resolved an approval card) and
``command`` (a slash command, answered without the text joining a turn
verbatim) never do, so the client settles those itself.
"""

import asyncio
import re
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from chief.adapters.base import Inject, Message
from chief.adapters.socket import HandleMessage
from chief.approvals import ApprovalBroker
from chief.persistence.store import MessageStore

# An iMessage group's thread_key is an opaque 32-char hex chat id; a 1:1 key is
# a phone/email handle. Groups have no core send path (imsg only).
_GROUP_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def build_send_route(
    store: MessageStore,
    default_channel: str,
    handle: HandleMessage,
    inject: Inject,
    approvals: ApprovalBroker,
    is_authed: Callable[[Request], bool],
    unauthorized: Callable[[], Response],
) -> Route:
    """``interrupt: true`` hands the text to the thread's in-flight turn (seen
    at its next tool boundary); without a running turn — or without the flag —
    it queues as an ordinary turn behind whatever is running."""

    async def send(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        body = await request.json()
        # The client sends the thread's real key; the channel is resolved from
        # the store (the device that owns the thread), never trusted from the
        # client. An unknown thread is a fresh web scratch buffer.
        thread_key = str(body.get("thread") or "web:main")
        channel = await store.channel(thread_key) or default_channel
        if channel == "imessage" and _GROUP_RE.match(thread_key):
            # A group's send path is imsg (owner-directed), not the core
            # one-to-one adapter; the cockpit keeps groups view-only.
            return PlainTextResponse(
                "group threads are view-only — send with imsg", status_code=400
            )
        text = str(body["text"])
        if approvals.resolve(thread_key, text):
            return PlainTextResponse("answered", status_code=202)
        message = Message(
            channel=channel, sender="owner", thread_key=thread_key, text=text
        )
        loop = asyncio.get_running_loop()

        def queue(queued: Message) -> None:
            loop.create_task(handle(queued))

        if text.startswith("/"):
            queue(message)
            return PlainTextResponse("command", status_code=202)
        if body.get("interrupt") is True and inject(message, queue):
            return PlainTextResponse("injected", status_code=202)
        queue(message)
        return PlainTextResponse("queued", status_code=202)

    return Route("/send", send, methods=["POST"])
