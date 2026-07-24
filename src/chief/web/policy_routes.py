"""Dashboard routes for a thread's stream policy (#265): the focused buffer
reads its resolved policy plus provenance (override vs channel default), and
flips it — the write lands on the session row, so the very next turn's
``dispatch._run_turn`` (which re-resolves fresh every turn) streams under it,
no restart needed. Split out of ``app.py`` to keep that file under the length
cap (#261 precedent).
"""

from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from chief.persistence.store import MessageStore
from chief.policy import COARSE, StreamPolicy, resolve

Unauthorized = Callable[[], Response]


def build_policy_routes(
    store: MessageStore,
    channel_defaults: dict[str, StreamPolicy],
    is_authed: Callable[[Request], bool],
    unauthorized: Unauthorized,
) -> list[Route]:
    async def get_policy(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        thread = request.query_params.get("thread", "")
        if not thread:
            return JSONResponse({"error": "thread required"}, status_code=400)
        channel = await store.channel(thread) or ""
        override = await store.stream_policy(thread)
        default = channel_defaults.get(channel, COARSE)
        return JSONResponse(
            {
                "channel": channel,
                "resolved": resolve(channel, override, channel_defaults).to_dict(),
                "default": default.to_dict(),
                "override": override,
            }
        )

    async def set_policy(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        body = await request.json()
        thread = str(body.get("thread") or "")
        if not thread:
            return PlainTextResponse("thread required", status_code=400)
        policy = body.get("policy")
        if policy is None:
            # Clear the override — the thread falls back to the channel default.
            await store.set_stream_policy(thread, None)
            return PlainTextResponse("", status_code=200)
        try:
            validated = StreamPolicy.from_dict(policy).to_dict()
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        await store.set_stream_policy(thread, validated)
        return PlainTextResponse("", status_code=200)

    return [
        Route("/policy", get_policy, methods=["GET"]),
        Route("/policy", set_policy, methods=["POST"]),
    ]
