"""The post_tool seam: screen a tool result before the model reads it.

A ``post_tool`` hook receives the :class:`ToolCall` and the result string a
tool returned, and answers with one of exactly two powers:

- :class:`Annotate` — prepend a name-attributed ``<hook>`` note to the
  **unchanged** payload (tag it untrusted, tell the model how to treat it).
- :class:`Veto` — withhold the payload entirely; the model gets the refusal
  and the hook's reason instead.

There is deliberately no third power. A hook returning a replacement string is
ignored: a buggy screener must not be able to silently corrupt a tool result.
The first veto wins and short-circuits the remaining hooks, so no later hook
can undo it. Failures are logged and dropped like every other hook kind.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chief.agent.loop import PostTool
from chief.hooks.runner import render_block
from chief.provider.base import ToolCall

if TYPE_CHECKING:
    from chief.hooks.registry import HookRegistry

VETO_NOTICE = "error: tool result withheld by a post_tool hook"


@dataclass(frozen=True)
class Annotate:
    """Keep the payload verbatim, prefixed with ``note`` as a <hook> block."""

    note: str


@dataclass(frozen=True)
class Veto:
    """Drop the payload; the model sees the refusal and ``reason`` instead."""

    reason: str


PostToolVerdict = Annotate | Veto | None
PostToolHook = Callable[[ToolCall, str], Awaitable[PostToolVerdict]]
PostToolEntry = tuple[str, PostToolHook]


async def run_post_tool(
    entries: list[PostToolEntry],
    call: ToolCall,
    result: str,
    timeout: float,
    logger: logging.Logger,
) -> str:
    """The text that actually reaches the model for this tool result.

    Hooks run in the registry's (package-name-sorted) order under ``timeout``.
    A raise, a timeout, or any non-verdict return value leaves the result
    untouched. The first :class:`Veto` returns immediately.
    """
    notes: list[tuple[str, str]] = []
    for name, fn in entries:
        try:
            verdict = await asyncio.wait_for(fn(call, result), timeout)
        except Exception:
            logger.error("post_tool hook %r failed; dropping", name, exc_info=True)
            continue
        if isinstance(verdict, Veto):
            return render_block(name, f"{VETO_NOTICE}: {verdict.reason}").lstrip()
        if isinstance(verdict, Annotate):
            notes.append((name, verdict.note))
    blocks = [render_block(name, note).lstrip() for name, note in notes]
    return "\n\n".join([*blocks, result])


def tool_screener(
    hooks: "HookRegistry | None", timeout: float, logger: logging.Logger
) -> PostTool | None:
    """The ``post_tool`` callable :func:`chief.agent.loop.run_turn` takes, or
    ``None`` when nothing is registered (the loop then skips the seam)."""
    if hooks is None:
        return None

    async def screen(call: ToolCall, result: str) -> str:
        return await run_post_tool(hooks.post_tool(), call, result, timeout, logger)

    return screen
