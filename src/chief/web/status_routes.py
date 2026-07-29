"""Read-only status routes for the statusbar: monitors, and the boot check.

``/posture`` is the web half of the boot check (#286) — automatic login is
documented to break silently after an OS update, so the owner must be able to
see chief's session state rather than notice its absence. Split out of
``app.py`` to keep that file under the length cap (#261 precedent).
"""

import getpass
import os
import sys
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from chief.install.posture import read_posture
from chief.monitors.service import MonitorService

Unauthorized = Callable[[], Response]


def build_status_routes(
    monitors: MonitorService,
    is_authed: Callable[[Request], bool],
    unauthorized: Unauthorized,
) -> list[Route]:
    async def monitor_list(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        rows = await monitors.list_monitors()
        if not rows:
            return PlainTextResponse("none")
        return PlainTextResponse(
            "; ".join(f"#{r.id} {r.description}" for r in rows)
        )

    async def posture(request: Request) -> Response:
        if not is_authed(request):
            return unauthorized()
        platform = "darwin" if sys.platform == "darwin" else "linux"
        # getpass, not os.getlogin(): a daemon has no controlling terminal.
        state = read_posture(
            platform=platform, user=getpass.getuser(), uid=os.getuid()
        )
        return PlainTextResponse(state.summary())

    return [
        Route("/monitors", monitor_list),
        Route("/posture", posture),
    ]
