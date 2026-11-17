"""The OAuth redirect landing: where a login that happens on this machine ends.

``GET /oauth/callback`` is deliberately open — the owner's browser arrives here
straight from the provider, with no session cookie — so the link's ``state``
(matched in constant time against the logins this daemon opened) is the only
thing that lets a request complete anything. The authorization code is never
echoed back.
"""

import html

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from chief.mcpclient.oauth import OAuthBroker
from chief.mcpclient.oauth_types import CALLBACK_PATH

_PAGE = "<!doctype html><title>chief</title><body><p>{}</p></body>"


def build_oauth_routes(broker: OAuthBroker) -> list[Route]:
    async def callback(request: Request) -> Response:
        if request.method != "GET":
            # Starlette adds HEAD to any GET route; a link scanner's prefetch
            # must not spend the login's one answer.
            return Response(status_code=405, headers={"Allow": "GET"})
        result = await broker.complete_login(str(request.url))
        if not result.ok:
            return HTMLResponse(_PAGE.format(html.escape(result.message)), 400)
        text = html.escape(f"{result.message} — you can close this tab.")
        return HTMLResponse(_PAGE.format(text))

    return [Route(CALLBACK_PATH, callback)]
