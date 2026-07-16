"""In-process uvicorn server for the web UI."""

import asyncio

import uvicorn
from starlette.applications import Starlette


class WebServer:
    """Serves the ASGI app inside the daemon's event loop."""

    def __init__(self, app: Starlette, host: str, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self) -> None:
        if self._task is not None:
            self._server.should_exit = True
            await self._task
