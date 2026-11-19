"""MCP server connections: discovery, tool registration, supervision.

Each server runs in its own background task (a ``Supervisor``) that holds the
transport open (spawning and supervising the child process for stdio) and
reconnects with backoff when it dies. Discovered tools register as
``mcp_<server>_<tool>``.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from chief.mcpclient.config import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ServerConfig,
    server_config_from_entry,
)
from chief.mcpclient.supervisor import Supervisor
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolContext, ToolRegistry

if TYPE_CHECKING:
    from chief.mcpclient.oauth import OAuthBroker

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "McpManager",
    "ServerConfig",
    "server_config_from_entry",
]


class McpManager:
    """Owns every MCP connection and its registered tools."""

    def __init__(
        self, registry: ToolRegistry, broker: "OAuthBroker | None" = None
    ) -> None:
        self._registry = registry
        self._broker = broker
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
        if config.auth == "oauth":
            if self._broker is None:
                raise ValueError(f"mcp server '{config.name}' needs an OAuth broker")
            self._broker.register(config)
        ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        link = Supervisor(config, self._register_tools, self._broker)
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
            if config.auth == "oauth" and self._broker is not None:
                self._broker.disabled(config.name)  # nothing is running it now
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

    def _register_tools(self, config: ServerConfig, listing: Any) -> int:
        """Register the server's tools that pass its filters; returns how many."""
        server = config.name
        tools = [tool for tool in listing.tools if config.tool_enabled(tool.name)]
        stale = config.unmatched_globs([tool.name for tool in listing.tools])
        if stale:
            # A typo'd include silently drops a tool the owner meant to keep.
            logger.warning(
                "mcp server %s: tool filter globs match no tool: %s",
                server, ", ".join(stale),
            )
        if len(tools) < len(listing.tools):
            logger.info(
                "mcp server %s: registered %d of %d tools (include/exclude_tools)",
                server, len(tools), len(listing.tools),
            )
        for tool in tools:
            spec = ToolSpec(
                name=f"mcp_{server}_{tool.name}",
                description=tool.description or "",
                parameters=tool.inputSchema,
            )
            props = (tool.inputSchema or {}).get("properties") or {}
            if config.session_context and "context" in props:
                logger.warning(
                    "mcp server %s tool %s takes an argument named 'context'; "
                    "session_context replaces it with chief's session context",
                    server, tool.name,
                )
            handler = self._make_handler(server, tool.name, config.session_context)
            self._registry.register(
                Tool(spec, handler, wants_context=config.session_context),
                replace=True,
            )
        return len(tools)

    def _make_handler(
        self, server: str, tool_name: str, session_context: bool = False
    ) -> Any:
        async def call(**arguments: Any) -> str:
            meta = None
            if session_context:
                context: ToolContext | None = arguments.pop("context", None)
                if context is not None:
                    meta = {
                        "chief/thread": context.thread_key,
                        "chief/channel": context.channel,
                    }
            link = self._links.get(server)
            if link is None:
                return f"error: mcp server '{server}' is not connected"
            return await link.call(tool_name, arguments, meta)

        return call
