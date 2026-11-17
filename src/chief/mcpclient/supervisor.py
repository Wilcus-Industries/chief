"""One MCP server's connection: open the transport, register its tools, and
reconnect with backoff when it dies."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from chief.mcpclient.config import ServerConfig

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 5.0

#: ``(server, tool listing) -> tool count`` — how a connection hands its
#: discovered tools to whoever owns the registry.
RegisterTools = Callable[[str, Any], int]


class Supervisor:
    """Keeps one server connected for as long as its task lives."""

    def __init__(self, config: ServerConfig, register_tools: RegisterTools) -> None:
        self.config = config
        self._register_tools = register_tools
        self._session: ClientSession | None = None

    async def supervise(self, ready: asyncio.Future[int]) -> None:
        config = self.config
        while True:
            try:
                await self._serve_once(ready)
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
            self._session = None
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _serve_once(self, ready: asyncio.Future[int]) -> None:
        config = self.config
        async with AsyncExitStack() as stack:
            if config.url is not None:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(config.url, headers=config.headers)
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
            self._session = session
            count = self._register_tools(config.name, await session.list_tools())
            if not ready.done():
                ready.set_result(count)
            logger.info("mcp server %s up with %d tools", config.name, count)
            await asyncio.Event().wait()  # hold the transport open until cancel

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and flatten its text content; failures are strings."""
        session = self._session
        if session is None:
            return f"error: mcp server '{self.config.name}' is not connected"
        result = await session.call_tool(tool_name, arguments)
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        return f"error: {text}" if result.isError else text
