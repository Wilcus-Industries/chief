"""Running one tool on a live MCP session."""

import asyncio
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.types import TextContent


@dataclass
class Live:
    """The open session of one connection, and a flag that it has ended."""

    session: ClientSession
    gone: asyncio.Event


async def call_tool(
    live: Live,
    tool_name: str,
    arguments: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> str | None:
    """The tool's text (``error: ...`` when it reports one), or ``None`` if the
    connection ended before it answered. ``meta`` rides on the request as its
    ``_meta`` (the calling chief session, for a ``session_context`` server).

    A request in flight when its transport dies is never answered — the SDK's
    session cannot deliver its "connection closed" error from inside the very
    teardown that raised — so without the race against ``gone`` a tool call
    would wait forever."""
    work = asyncio.ensure_future(
        live.session.call_tool(tool_name, arguments, meta=meta)
    )
    watch = asyncio.ensure_future(live.gone.wait())
    try:
        await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        watch.cancel()
        if not work.done():
            work.cancel()
    if not work.done() or work.cancelled():
        return None
    result = work.result()
    text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    return f"error: {text}" if result.isError else text
