"""MCP server connections: discovery, tool registration, supervision.

Each server runs in its own background task (a ``Supervisor``) that holds the
transport open (spawning and supervising the child process for stdio) and
reconnects with backoff when it dies. Discovered tools register as
``mcp_<server>_<tool>``.
"""

import asyncio
import logging
from typing import Any

from chief.mcpclient.config import ServerConfig as ServerConfig
from chief.mcpclient.config import server_config_from_entry as server_config_from_entry
from chief.mcpclient.supervisor import Supervisor
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolRegistry

logger = logging.getLogger(__name__)


class McpManager:
    """Owns every MCP connection and its registered tools."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._links: dict[str, Supervisor] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def connect(self, config: ServerConfig) -> int:
        """Start supervising a server; returns its tool count once ready.

        A server that never reaches ``ready`` within ``config.timeout`` fails
        loudly and its supervising task is cancelled and awaited before this
        raises — otherwise that task would keep retrying forever, leaked,
        with nothing left holding a reference to stop it.
        """
        if config.name in self._tasks:
            raise ValueError(f"mcp server '{config.name}' already connected")
        ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        link = Supervisor(config, self._register_tools)
        self._links[config.name] = link
        task = asyncio.create_task(link.supervise(ready))
        self._tasks[config.name] = task
        try:
            return await asyncio.wait_for(ready, config.timeout)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(config.name, None)
            self._links.pop(config.name, None)
            raise TimeoutError(
                f"mcp server {config.name} did not connect within "
                f"{config.timeout:g}s"
            ) from None

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._links.clear()

    def _register_tools(self, server: str, listing: Any) -> int:
        for tool in listing.tools:
            spec = ToolSpec(
                name=f"mcp_{server}_{tool.name}",
                description=tool.description or "",
                parameters=tool.inputSchema,
            )
            self._registry.register(
                Tool(spec, self._make_handler(server, tool.name)), replace=True
            )
        return len(listing.tools)

    def _make_handler(self, server: str, tool_name: str) -> Any:
        async def call(**arguments: Any) -> str:
            link = self._links.get(server)
            if link is None:
                return f"error: mcp server '{server}' is not connected"
            return await link.call(tool_name, arguments)

        return call
