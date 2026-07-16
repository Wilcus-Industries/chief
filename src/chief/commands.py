"""Owner slash commands: a deterministic escape hatch on every adapter.

Parsed before agent dispatch, so they work even when a session is wedged or
burning money. No model call is ever involved.
"""

from collections.abc import Awaitable, Callable

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.cron.service import CronService
from chief.monitors.service import MonitorService

CommandHandler = Callable[[str, Message], Awaitable[str]]


class CommandSet:
    """The built-in owner commands; packages/self-edit can register more."""

    def __init__(
        self,
        manager: SessionManager,
        monitors: MonitorService,
        cron: CronService,
    ) -> None:
        self._manager = manager
        self._monitors = monitors
        self._cron = cron
        self._commands: dict[str, CommandHandler] = {
            "help": self._help,
            "monitors": self._list_monitors,
            "schedules": self._list_schedules,
            "model": self._model,
        }

    def register(self, name: str, handler: CommandHandler) -> None:
        self._commands[name] = handler

    async def run(self, message: Message) -> str | None:
        """Handle a "/command args" message; None means not a command."""
        if not message.text.startswith("/"):
            return None
        name, _, args = message.text[1:].partition(" ")
        handler = self._commands.get(name)
        if handler is None:
            return f"unknown command /{name} — try /help"
        return await handler(args.strip(), message)

    async def _help(self, args: str, message: Message) -> str:
        names = ", ".join(f"/{n}" for n in sorted(self._commands))
        return f"commands: {names}"

    async def _list_monitors(self, args: str, message: Message) -> str:
        rows = await self._monitors.list_enabled()
        if not rows:
            return "no monitors"
        return "\n".join(
            f"#{r.id} [{r.watch_channel}] {r.description}" for r in rows
        )

    async def _list_schedules(self, args: str, message: Message) -> str:
        rows = await self._cron.list_enabled()
        if not rows:
            return "no schedules"
        return "\n".join(f"#{r.id} [{r.spec}] {r.description}" for r in rows)

    async def _model(self, args: str, message: Message) -> str:
        session = await self._manager.get_or_create(
            message.thread_key, message.channel
        )
        if not args:
            return f"model: {session.model}"
        await self._manager.set_model(message.thread_key, message.channel, args)
        return f"model set to {args} for this thread"
