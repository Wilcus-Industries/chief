"""Resilient hook execution, the <hook> render format, and system assembly.

Every hook runs under a timeout; a slow or raising hook is logged at ERROR and
dropped, never propagated into the turn. Context hooks (pre_turn,
session_start) return text collected as ``(package, text)``; observers
(post_turn) return nothing. ``assemble_system`` composes the per-turn system
message so the session stays thin.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from chief.agent.loop import TurnResult

if TYPE_CHECKING:
    from chief.hooks.registry import HookRegistry

ContextEntry = tuple[str, Callable[[], Awaitable[str | None]]]
PostEntry = tuple[str, Callable[[TurnResult, list[dict[str, Any]]], Awaitable[None]]]


async def run_context_hooks(
    entries: list[ContextEntry], timeout: float, logger: logging.Logger
) -> list[tuple[str, str]]:
    """Run each context hook under ``timeout``; collect non-empty (name, text).

    A hook that raises or exceeds the timeout is logged and dropped; the other
    contributions are unaffected. Used for both pre_turn and session_start.
    """
    contributions: list[tuple[str, str]] = []
    for name, fn in entries:
        try:
            text = await asyncio.wait_for(fn(), timeout)
        except Exception:
            logger.error("hook %r failed; dropping contribution", name, exc_info=True)
            continue
        if text:
            contributions.append((name, text))
    return contributions


async def run_post_turn(
    entries: list[PostEntry],
    result: TurnResult,
    messages: list[dict[str, Any]],
    timeout: float,
    logger: logging.Logger,
) -> None:
    """Run each post_turn observer under ``timeout``; log and drop failures."""
    for name, fn in entries:
        try:
            await asyncio.wait_for(fn(result, messages), timeout)
        except Exception:
            logger.error("post_turn hook %r failed", name, exc_info=True)


def render_block(package: str, text: str) -> str:
    """The delimited, name-attributed block a context contribution renders to."""
    return f'\n\n<hook source="{package}">\n{text}\n</hook>'


async def assemble_system(
    *,
    base: str,
    soul_reader: Callable[[], str],
    hooks: "HookRegistry | None",
    first_turn: bool,
    timeout: float,
    logger: logging.Logger,
) -> str:
    """Compose the per-turn system message.

    Soul on top (reserved: its legacy ``{soul}\\n\\n{base}`` placement, never a
    <hook> block, but run through the same resilient runner), then base + origin
    note, then each package contribution as a name-sorted <hook> block.
    """
    soul = await run_context_hooks([("soul", _sync(soul_reader))], timeout, logger)
    system = f"{soul[0][1]}\n\n{base}" if soul else base
    if hooks is None:
        return system
    blocks = await run_context_hooks(hooks.pre_turn(), timeout, logger)
    if first_turn:
        blocks += await run_context_hooks(hooks.session_start(), timeout, logger)
    for package, text in sorted(blocks, key=lambda block: block[0]):
        system += render_block(package, text)
    return system


def _sync(reader: Callable[[], str]) -> Callable[[], Awaitable[str | None]]:
    """Adapt the sync soul reader onto the async context-hook seam."""

    async def read() -> str | None:
        return reader()

    return read
