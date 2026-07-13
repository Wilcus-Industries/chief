"""The embedded HTTP listener (#153): uvicorn hosted on the daemon's own loop.

:class:`WebServer` mirrors the :class:`~chief.client_plane.SocketServer` lifecycle
shape — ``run()`` blocks serving until ``stop()``, ``started`` signals readiness — so
``app.serve`` gathers it exactly like the adapters and the socket listener.
"""

import asyncio
import contextlib
from collections.abc import Generator

import uvicorn
from starlette.types import ASGIApp


class _SignalFreeServer(uvicorn.Server):
    """uvicorn without its signal capture — the daemon owns SIGINT/SIGTERM.

    Stock ``Server.serve`` installs its own handlers on the main thread, which would
    swallow Ctrl-C for the whole daemon (uvicorn would exit; the gather's other
    coroutines would keep running, deaf to the signal). Shutdown is driven by
    :meth:`WebServer.stop` setting ``should_exit`` instead.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class WebServer:
    """Serve one ASGI app on the configured bind; run/stop like an adapter."""

    def __init__(self, app: ASGIApp, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.started = asyncio.Event()
        self._uvicorn = _SignalFreeServer(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                # The daemon configured logging already (obs.logging); uvicorn must
                # not re-configure the root logger or double-log every request.
                log_config=None,
                access_log=False,
                lifespan="off",
            )
        )

    @property
    def bound_port(self) -> int:
        """The actual listening port — meaningful once :attr:`started` is set.

        ``port=0`` binds an ephemeral port (used by tests); this reads the real one
        back off the listening socket.
        """
        servers = self._uvicorn.servers
        assert servers, "bound_port read before the server started"
        sockets = servers[0].sockets
        assert sockets, "server has no bound socket"
        return int(sockets[0].getsockname()[1])

    async def run(self) -> None:
        """Serve until :meth:`stop`. Sets :attr:`started` once the bind is live."""
        serve = asyncio.create_task(self._uvicorn.serve())
        try:
            while not self._uvicorn.started and not serve.done():
                await asyncio.sleep(0.02)
            self.started.set()
            await serve
        finally:
            # A cancelled run() (gather teardown) must still stop uvicorn cleanly.
            self._uvicorn.should_exit = True
            if not serve.done():
                await serve

    async def stop(self) -> None:
        """Ask uvicorn to exit; ``run()`` returns once shutdown completes."""
        self._uvicorn.should_exit = True
