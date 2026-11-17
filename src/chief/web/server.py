"""In-process uvicorn server for the web UI."""

import asyncio

import uvicorn
from starlette.applications import Starlette


class WebServer:
    """Serves the ASGI app inside the daemon's event loop."""

    # Bounds graceful shutdown: uvicorn otherwise waits for every open
    # connection to finish, so one long-lived client could wedge a restart.
    graceful_shutdown_seconds = 5

    def __init__(self, app: Starlette, host: str, port: int) -> None:
        # Kept so an integration test can drive the production-wired ASGI app
        # in-process (httpx ASGITransport) without a real socket (#277).
        self.app = app
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                timeout_graceful_shutdown=self.graceful_shutdown_seconds,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self) -> None:
        if self._task is not None:
            self._server.should_exit = True
            await self._task
