"""MCP server connections: discovery, tool registration, supervision.

Each server runs in its own background task that holds the transport open
(spawning and supervising the child process for stdio) and reconnects with
backoff when it dies. Discovered tools register as ``mcp_<server>_<tool>``.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolSpec

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 5.0


@dataclass(frozen=True)
class ServerConfig:
    """One MCP server: exactly one of url (HTTP) or command (stdio).

    ``env`` and ``cwd`` apply to stdio servers only. Environment is opt-in:
    the child gets the SDK's minimal inherited set (``PATH``, `HOME`, etc.)
    plus exactly the keys in ``env`` — never the daemon's full environment —
    so a package can hand a server a credentials path without a wrapper
    script and an operator can audit the grant by reading ``config.yaml``.
    """

    name: str
    url: str | None = None
    command: tuple[str, ...] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None


class McpManager:
    """Owns every MCP connection and its registered tools."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._sessions: dict[str, ClientSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def connect(self, config: ServerConfig, timeout: float = 30.0) -> int:
        """Start supervising a server; returns its tool count once ready."""
        if config.name in self._tasks:
            raise ValueError(f"mcp server '{config.name}' already connected")
        ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._tasks[config.name] = asyncio.create_task(
            self._supervise(config, ready)
        )
        return await asyncio.wait_for(ready, timeout)

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._sessions.clear()

    async def _supervise(
        self, config: ServerConfig, ready: asyncio.Future[int]
    ) -> None:
        while True:
            try:
                await self._serve_once(config, ready)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "mcp server %s connection failed; retrying in %ss",
                    config.name,
                    RECONNECT_DELAY_SECONDS,
                )
                if not ready.done():
                    ready.set_exception(
                        RuntimeError(f"mcp server {config.name} failed to start")
                    )
                    return
            self._sessions.pop(config.name, None)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _serve_once(
        self, config: ServerConfig, ready: asyncio.Future[int]
    ) -> None:
        async with AsyncExitStack() as stack:
            if config.url is not None:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(config.url)
                )
            elif config.command:
                params = StdioServerParameters(
                    command=config.command[0],
                    args=list(config.command[1:]),
                    env=config.env,
                    cwd=config.cwd,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                raise ValueError(f"mcp server {config.name} has no url or command")
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[config.name] = session
            count = self._register_tools(config.name, await session.list_tools())
            if not ready.done():
                ready.set_result(count)
            logger.info("mcp server %s up with %d tools", config.name, count)
            await asyncio.Event().wait()  # hold the transport open until cancel

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
            session = self._sessions.get(server)
            if session is None:
                return f"error: mcp server '{server}' is not connected"
            result = await session.call_tool(tool_name, arguments)
            parts = [
                c.text for c in result.content if isinstance(c, TextContent)
            ]
            text = "\n".join(parts)
            if result.isError:
                return f"error: {text}"
            return text

        return call
