"""Platform-neutral owner command registry (#129).

Every owner slash-command (``/cancel``, ``/tasks``, ``/close``, ``/rename``,
``/memory``, ``/forget``, ``/branch``, ``/opus``, ``/sonnet``, ``/route``) is
defined **once** here, over the
shared :class:`~chief.adapters.base.Engine` / :class:`~chief.adapters.base.MemoryReader`
interfaces. Each platform adapter (Telegram, Discord, …) parses its native update
into a :class:`CommandContext` and dispatches through :data:`OWNER_COMMANDS` — a
command added once here becomes available on every adapter that binds the
registry, with no adapter-side change.

This module imports from :mod:`.base`, never the other way — adapters import both.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..memory.store import OWNER_NAMESPACE
from .base import Engine, MemoryReader, ReplyFn, default_branch_title


@dataclass(frozen=True)
class CommandContext:
    """Everything a command handler needs, normalized off the native update."""

    engine: Engine
    memory: MemoryReader | None
    thread_key: str
    arg: str  # already stripped
    is_casual: bool
    reply: ReplyFn


#: A registered command handler. Named ``CommandFn``, not ``CommandHandler`` — the
#: latter clashes with ``telegram.ext.CommandHandler``.
CommandFn = Callable[[CommandContext], Awaitable[None]]


async def _cmd_cancel(ctx: CommandContext) -> None:
    stopped = await ctx.engine.cancel(ctx.thread_key)
    await ctx.reply("Cancelled." if stopped else "Nothing running here.")


async def _cmd_tasks(ctx: CommandContext) -> None:
    tasks = await ctx.engine.active_tasks()
    if not tasks:
        await ctx.reply("No active tasks.")
        return
    lines = [f"• {t.title or t.thread_key} — {t.status}" for t in tasks]
    await ctx.reply("\n".join(lines))


async def _cmd_close(ctx: CommandContext) -> None:
    """Finish this thread now: mark it done and archive it (owner only)."""
    if ctx.is_casual:
        # The casual lane self-compacts and deliberately never archives — /close
        # must not become the back door around that.
        await ctx.reply(
            "The casual channel stays open — /close only works in a task thread."
        )
        return
    await ctx.reply(await ctx.engine.close(ctx.thread_key))


async def _cmd_rename(ctx: CommandContext) -> None:
    """Retitle this thread in every listing (owner only)."""
    if not ctx.arg:
        await ctx.reply("Usage: /rename <title>")
        return
    await ctx.reply(await ctx.engine.rename(ctx.thread_key, ctx.arg))


async def _cmd_memory(ctx: CommandContext) -> None:
    """List the owner's stored facts (owner only)."""
    if ctx.memory is None:
        await ctx.reply("Memory isn't enabled.")
        return
    facts = ctx.memory.list_facts(OWNER_NAMESPACE)
    if not facts:
        await ctx.reply("No memories yet.")
        return
    await ctx.reply("\n".join(f"• {f.title}" for f in facts))


async def _cmd_forget(ctx: CommandContext) -> None:
    """Forget owner facts matching the command argument (owner only)."""
    if ctx.memory is None:
        await ctx.reply("Memory isn't enabled.")
        return
    if not ctx.arg:
        await ctx.reply("Usage: /forget <text>")
        return
    removed = await ctx.memory.forget(OWNER_NAMESPACE, ctx.arg)
    if not removed:
        await ctx.reply("Nothing matched.")
        return
    await ctx.reply("Forgot: " + ", ".join(f.title for f in removed))


async def _cmd_branch(ctx: CommandContext) -> None:
    """Promote the casual channel into a tracked thread (owner + casual only).

    Only the casual lane carries the lossy self-compacting context worth promoting;
    a real tracked thread is a no-op reply. The optional argument names the new
    thread; otherwise a timestamp does.
    """
    if not ctx.is_casual:
        await ctx.reply("/branch only works in the casual channel.")
        return
    title = ctx.arg or default_branch_title()
    await ctx.engine.branch(ctx.thread_key, title)
    await ctx.reply(f'→ Branched into "{title}".')


async def _cmd_opus(ctx: CommandContext) -> None:
    """Escalate this thread to Opus (owner only) — pre-approved, no card (M11)."""
    await ctx.reply(await ctx.engine.escalate(ctx.thread_key))


async def _cmd_sonnet(ctx: CommandContext) -> None:
    """Revert this thread to the default model (owner only) (M11)."""
    await ctx.reply(await ctx.engine.revert(ctx.thread_key))


async def _cmd_route(ctx: CommandContext) -> None:
    """Override this thread's routing category (owner only, #79).

    ``/route <category>`` pins the job category (and respawns the session on that
    category's target); a bare ``/route`` prints usage.
    """
    if not ctx.arg:
        await ctx.reply("Usage: /route <category>")
        return
    await ctx.reply(await ctx.engine.route(ctx.thread_key, ctx.arg))


class CommandRegistry:
    """A platform-neutral table of owner command name → handler (+ description)."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandFn] = {}
        self._descriptions: dict[str, str] = {}

    def register(self, name: str, handler: CommandFn, description: str = "") -> None:
        self._commands[name] = handler
        self._descriptions[name] = description

    def names(self) -> list[str]:
        return list(self._commands)

    def entries(self) -> list[tuple[str, str]]:
        """Every ``(name, description)`` pair, in registration order — the one
        source both clients' autocomplete menus describe commands from."""
        return [(name, self._descriptions[name]) for name in self._commands]

    async def dispatch(self, name: str, ctx: CommandContext) -> None:
        """Run ``name``'s handler, or silently no-op if unknown (both adapters)."""
        handler = self._commands.get(name)
        if handler is None:
            return
        await handler(ctx)


def owner_registry() -> CommandRegistry:
    """Build a fresh registry carrying every owner command."""
    registry = CommandRegistry()
    registry.register("cancel", _cmd_cancel, "Stop this thread's running task")
    registry.register("tasks", _cmd_tasks, "List active tasks")
    registry.register("close", _cmd_close, "Finish and archive this thread")
    registry.register("rename", _cmd_rename, "Retitle this thread: /rename <title>")
    registry.register("memory", _cmd_memory, "List stored facts")
    registry.register("forget", _cmd_forget, "Forget matching facts: /forget <text>")
    registry.register("branch", _cmd_branch, "Promote casual chat into a thread")
    registry.register("opus", _cmd_opus, "Escalate this thread to Opus")
    registry.register("sonnet", _cmd_sonnet, "Revert this thread to the default model")
    registry.register("route", _cmd_route, "Pin a routing category: /route <category>")
    return registry


#: Production default singleton — both adapters bind this unless a test injects
#: its own registry.
OWNER_COMMANDS = owner_registry()
