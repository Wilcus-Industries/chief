"""The assembled daemon and its lifecycle (built by chief.app.build_app)."""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.imessage import IMessageAdapter
from chief.adapters.socket import SocketAdapter
from chief.bus import EventBus
from chief.config import Config
from chief.cron.service import CronService
from chief.dispatch import Dispatcher
from chief.hub import ObserverHub
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.monitors.service import MonitorService
from chief.persistence.store import MessageStore
from chief.tools.shell.service import ShellService
from chief.web.adapter import WebAdapter
from chief.web.server import WebServer

logger = logging.getLogger(__name__)


@dataclass
class App:
    """The assembled daemon and its lifecycle."""

    config: Config
    engine: AsyncEngine
    store: MessageStore
    dispatcher: Dispatcher
    socket_adapter: SocketAdapter
    monitor_service: MonitorService
    cron_service: CronService
    bus: EventBus
    hub: ObserverHub
    web_adapter: WebAdapter
    web_server: WebServer | None
    mcp_manager: McpManager
    mcp_configs: tuple[ServerConfig, ...]
    imessage_adapter: IMessageAdapter | None
    shell_service: ShellService

    async def start(self) -> None:
        await self.socket_adapter.start()
        await self.web_adapter.start()
        if self.imessage_adapter is not None:
            await self.imessage_adapter.start()
        self.cron_service.start()
        if self.web_server is not None:
            await self.web_server.start()
        for server in self.mcp_configs:
            try:
                await self.mcp_manager.connect(server)
            except Exception:
                # A dead sidecar must not keep the whole daemon down.
                logger.exception("mcp server %s failed to connect", server.name)

    async def stop(self) -> None:
        await self.mcp_manager.stop()
        if self.web_server is not None:
            await self.web_server.stop()
        await self.cron_service.stop()
        if self.imessage_adapter is not None:
            await self.imessage_adapter.stop()
        self.hub.close()
        await self.web_adapter.stop()
        await self.socket_adapter.stop()
        await self.shell_service.aclose()
        await self.engine.dispose()
