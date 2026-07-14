"""Boot wiring for the web UI (#153): one buildable, gatherable stack.

:func:`build_web_stack` assembles the production web surface — the authenticator over
the secrets dir, the client-plane bridge onto the daemon's own socket, the ASGI app,
and the embedded HTTP listener — into a :class:`WebStack` whose ``run()``/``stop()``
mirror the adapter lifecycle, so ``chief.app.serve`` gathers it exactly like the
adapters, the scheduler, and the socket server.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from .app import IMessagePanel, WatchesPanel, WebDeps, build_web_app
from .auth import WebAuth
from .bridge import SocketBridge
from .files import FileAreas
from .health import HealthCheck, build_health_checks
from .server import WebServer
from .settings_io import OwnerConfig, SecretsStore, SettingsPanel

#: The owner config file the curated settings forms write. Relative on purpose —
#: resolved against the process cwd, exactly how pydantic-settings finds it.
OWNER_CONFIG_PATH = Path("config.yaml")


@dataclass
class WebStack:
    """The web surface's two long-lived parts, run and stopped as one."""

    server: WebServer
    bridge: SocketBridge

    async def run(self) -> None:
        """Attach to the client plane (retrying while it binds), then serve."""
        await self.bridge.start()
        await self.server.run()

    async def stop(self) -> None:
        """Stop serving, then detach from the socket; idempotent."""
        await self.server.stop()
        await self.bridge.stop()


def build_web_stack(
    settings: Settings,
    *,
    secrets_dir: Path,
    config_path: Path = OWNER_CONFIG_PATH,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    extra_health: Sequence[HealthCheck] = (),
) -> WebStack:
    """Build the production web stack; pure construction, no I/O until ``run``.

    ``session_factory`` powers the iMessage whitelist panel (#156) — DB-backed,
    unlike the file-backed curated settings; absent (or with the adapter off) the
    panel is hidden. ``extra_health`` appends live checks the caller owns (the
    iMessage poller state) onto the settings-derived checklist.
    """
    auth = WebAuth(secrets_dir)
    bridge = SocketBridge(settings.socket_path)
    # The workspace area is always exposed (day-one uploads need somewhere to land,
    # and workspace_dir is always configured); screenshots only exist alongside the
    # playwright sidecar that produces them.
    files = FileAreas(
        workspace=Path(settings.workspace_dir),
        screenshots=(
            Path(settings.playwright_screenshots_dir)
            if settings.playwright_enabled
            else None
        ),
    )
    panel = SettingsPanel(
        secrets=SecretsStore(secrets_dir),
        config=OwnerConfig(config_path),
        settings=settings,
    )
    imessage = (
        IMessagePanel(session_factory=session_factory)
        if session_factory is not None and settings.imessage_enabled
        else None
    )
    # Watches (#165) are meaningless without the iMessage adapter — same gate as the
    # owner's watch tools (settings.imessage_configured).
    watches = (
        WatchesPanel(session_factory=session_factory)
        if session_factory is not None and settings.imessage_configured
        else None
    )
    app = build_web_app(
        WebDeps(
            auth=auth,
            bridge=bridge,
            files=files,
            settings_panel=panel,
            imessage=imessage,
            watches=watches,
            health=tuple(build_health_checks(settings)) + tuple(extra_health),
        )
    )
    server = WebServer(app, host=settings.web_host, port=settings.web_port)
    return WebStack(server=server, bridge=bridge)
