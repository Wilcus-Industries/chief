"""Resilient hook execution, the <hook> render format, and system assembly.

Every hook runs under a timeout; a slow or raising hook is logged at ERROR and
dropped, never propagated into the turn. Context hooks (pre_turn,
session_start) return text collected as ``(package, text)``; observers
(post_turn) return nothing. ``assemble_system`` composes the per-turn system
message so the session stays thin.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from chief.agent.loop import TurnResult
from chief.hooks.context import TurnContext

if TYPE_CHECKING:
    from chief.hooks.registry import HookRegistry

ContextEntry = tuple[str, Callable[[TurnContext], Awaitable[str | None]]]
PostEntry = tuple[str, Callable[[TurnResult, list[dict[str, Any]]], Awaitable[None]]]


async def run_context_hooks(
    entries: list[ContextEntry],
    turn: TurnContext,
    timeout: float,
    logger: logging.Logger,
) -> list[tuple[str, str]]:
    """Run each context hook under ``timeout``; collect non-empty (name, text).

    Each hook is handed the per-turn :class:`TurnContext`. A hook that raises
    or exceeds the timeout is logged and dropped; the other contributions are
    unaffected. Used for both pre_turn and session_start.
    """
    contributions: list[tuple[str, str]] = []
    for name, fn in entries:
        try:
            text = await asyncio.wait_for(fn(turn), timeout)
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


# The block delimiter and the name-attribution attribute are trusted structure;
# a contribution's text/name must never be able to forge either. Contributed
# text may be attacker-influenced (retrieved/relayed content, e.g. #207), so a
# ``<hook``/``</hook`` in it is neutralized and a name is reduced to a slug.
_HOOK_TAG = re.compile(r"<(/?hook)", re.IGNORECASE)
_UNSAFE_NAME_CHAR = re.compile(r"[^a-zA-Z0-9._-]")


def _escape_hook_tags(text: str) -> str:
    """Entity-escape only ``<hook``/``</hook`` sequences so a contribution can't
    break out of or forge a delimiter. All other text passes through verbatim."""
    return _HOOK_TAG.sub(r"&lt;\1", text)


def sanitize_package_name(name: str) -> str:
    """Reduce a package name to the delimiter- and path-safe slug charset
    ``[a-zA-Z0-9._-]``, dropping anything else — so a crafted name can neither
    break the ``source="..."`` attribute nor carry a path separator."""
    return _UNSAFE_NAME_CHAR.sub("", name)


def is_safe_package_name(name: str) -> bool:
    """True only if ``name`` is already a clean slug and not a traversal
    component — the loader skips packages that fail this before any mkdir."""
    return (
        bool(name)
        and name == sanitize_package_name(name)
        and name not in (".", "..")
    )


def render_block(package: str, text: str) -> str:
    """The delimited, name-attributed block a context contribution renders to.

    The block format is fixed structure; the contributed ``package`` and
    ``text`` are sanitized so neither can forge attribution or a delimiter.
    """
    source = sanitize_package_name(package)
    body = _escape_hook_tags(text)
    return f'\n\n<hook source="{source}">\n{body}\n</hook>'


async def assemble_system(
    *,
    base: str,
    soul_reader: Callable[[], str],
    hooks: "HookRegistry | None",
    turn: TurnContext,
    first_turn: bool,
    timeout: float,
    logger: logging.Logger,
) -> str:
    """Compose the per-turn system message.

    Soul on top (reserved: its legacy ``{soul}\\n\\n{base}`` placement, never a
    <hook> block, but run through the same resilient runner), then base + origin
    note, then each package contribution as a name-sorted <hook> block. ``turn``
    is forwarded to every context hook.
    """
    soul = await run_context_hooks(
        [("soul", _sync(soul_reader))], turn, timeout, logger
    )
    system = f"{soul[0][1]}\n\n{base}" if soul else base
    if hooks is None:
        return system
    blocks = await run_context_hooks(hooks.pre_turn(), turn, timeout, logger)
    if first_turn:
        blocks += await run_context_hooks(
            hooks.session_start(), turn, timeout, logger
        )
    for package, text in sorted(blocks, key=lambda block: block[0]):
        system += render_block(package, text)
    return system


def _sync(
    reader: Callable[[], str],
) -> Callable[[TurnContext], Awaitable[str | None]]:
    """Adapt the sync soul reader onto the async context-hook seam.

    The soul ignores the turn, but must match the one-arg hook signature.
    """

    async def read(_turn: TurnContext) -> str | None:
        return reader()

    return read
