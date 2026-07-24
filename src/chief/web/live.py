"""The dashboard's live surface: the SSE stream and the card-answer route.

Split out of ``app.py`` to keep that file under the length cap (#261
precedent) — ``/events`` and ``/approve`` share the hub and the approvals
broker and belong together (#267).
"""

import json
from collections.abc import AsyncIterator, Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from chief.approvals import ApprovalBroker
from chief.hub import ObserverHub

Unauthorized = Callable[[], Response]


def build_live_routes(
    hub: ObserverHub,
    approvals: ApprovalBroker,
    is_authed: Callable[[Request], bool],
    unauthorized: Unauthorized,
) -> list[Route]:
    """``/approve`` resolves the same broker a gray-zone card's ``ask`` is
    waiting on — first answer wins, whether it lands here or on the origin
    channel; a second (or too-late) answer is a no-op, reported as 409.
    ``/events`` replays a still-pending card to a late-joining watcher — a
    blocked, watched thread must never stall silently (#267 AC4).
    """

    async def approve(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        body = await request.json()
        thread = str(body.get("thread") or "")
        answer = str(body.get("answer") or "")
        if not thread or not approvals.resolve(thread, answer):
            return PlainTextResponse("no pending card", status_code=409)
        return PlainTextResponse("", status_code=200)

    async def events(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        thread = request.query_params.get("thread") or None
        queue = hub.listen(thread)
        if thread is not None:
            question = approvals.pending_question(thread)
            if question is not None:
                queue.put_nowait(
                    {"type": "approval", "thread": thread, "question": question}
                )

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

    return [
        Route("/approve", approve, methods=["POST"]),
        Route("/events", events),
    ]
