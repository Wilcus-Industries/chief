"""MCP server connections: discovery, tool registration, supervision.

Each server runs in its own background task that holds the transport open
(spawning and supervising the child process for stdio) and reconnects with
backoff when it dies. Discovered tools register as ``mcp_<server>_<tool>``.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from chief.mcpclient.config import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ServerConfig,
    server_config_from_entry,
)
from chief.provider.base import ToolSpec
from chief.tools import Tool, ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "McpManager",
    "ServerConfig",
    "server_config_from_entry",
]

RECONNECT_DELAY_SECONDS = 5.0


class McpManager:
    """Owns every MCP connection and its registered tools."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._sessions: dict[str, ClientSession] = {}
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
        task = asyncio.create_task(self._supervise(config, ready))
        self._tasks[config.name] = task
        try:
            return await asyncio.wait_for(ready, config.timeout)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(config.name, None)
            self._sessions.pop(config.name, None)
            raise TimeoutError(
                f"mcp server {config.name} did not connect within "
                f"{config.timeout:g}s"
            ) from None

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
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    # A cancel delivered mid-teardown can surface as an
                    # ordinary exception instead of CancelledError itself —
                    # e.g. a stdio transport's task group wraps a sibling's
                    # BrokenResourceError around the very cancellation that
                    # caused it. Honor the pending cancel rather than
                    # treating this as a connection failure to retry, or the
                    # retry loop spins up a fresh, uncancellable subprocess
                    # (connect()'s single task.cancel() is already spent).
                    raise asyncio.CancelledError() from None
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
            count = self._register_tools(config, await session.list_tools())
            if not ready.done():
                ready.set_result(count)
            logger.info("mcp server %s up with %d tools", config.name, count)
            await asyncio.Event().wait()  # hold the transport open until cancel

    def _register_tools(self, config: ServerConfig, listing: Any) -> int:
        server = config.name
        for tool in listing.tools:
            spec = ToolSpec(
                name=f"mcp_{server}_{tool.name}",
                description=tool.description or "",
                parameters=tool.inputSchema,
            )
            handler = self._make_handler(server, tool.name, config.session_context)
            self._registry.register(
                Tool(spec, handler, wants_context=config.session_context),
                replace=True,
            )
        return len(listing.tools)

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
            session = self._sessions.get(server)
            if session is None:
                return f"error: mcp server '{server}' is not connected"
            result = await session.call_tool(tool_name, arguments, meta=meta)
            parts = [
                c.text for c in result.content if isinstance(c, TextContent)
            ]
            text = "\n".join(parts)
            if result.isError:
                return f"error: {text}"
            return text

        return call
