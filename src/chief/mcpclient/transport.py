"""Opening an MCP server's transport and a session on it."""

from contextlib import AsyncExitStack

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from chief.mcpclient.config import ServerConfig


async def open_session(
    stack: AsyncExitStack, config: ServerConfig, auth: httpx.Auth | None = None
) -> ClientSession:
    """Open ``config``'s transport and an initialized session on it.

    Both close with ``stack``. ``auth`` rides on a url server's requests (the
    OAuth provider); a stdio server has no use for it."""
    if config.url is not None:
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(config.url, headers=config.headers, auth=auth)
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
    return session
