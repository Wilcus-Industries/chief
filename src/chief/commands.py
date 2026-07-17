"""Owner slash commands: a deterministic escape hatch on every adapter.

Parsed before agent dispatch, so they work even when a session is wedged or
burning money. No model call is ever involved.
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace

from chief.adapters.base import Message
from chief.agent.manager import SessionManager
from chief.cron.service import CronService
from chief.monitors.service import MonitorService
from chief.skills import SkillLibrary

CommandHandler = Callable[[str, Message], Awaitable[str]]


class CommandSet:
    """The built-in owner commands; packages/self-edit can register more."""

    def __init__(
        self,
        manager: SessionManager,
        monitors: MonitorService,
        cron: CronService,
        skills: SkillLibrary | None = None,
    ) -> None:
        self._manager = manager
        self._monitors = monitors
        self._cron = cron
        self._skills = skills
        self._commands: dict[str, CommandHandler] = {
            "help": self._help,
            "monitors": self._list_monitors,
            "schedules": self._list_schedules,
            "model": self._model,
        }

    def register(self, name: str, handler: CommandHandler) -> None:
        self._commands[name] = handler

    def palette(self) -> list[str]:
        """Every invocable name as ``/name`` — built-ins plus loadable skills.

        Feeds the web UI's completion menu; skills are live-scanned so newly
        installed ones show up without a restart.
        """
        names = set(self._commands)
        if self._skills is not None:
            names.update(skill.name for skill in self._skills.scan())
        return sorted(f"/{name}" for name in names)

    async def run(self, message: Message) -> str | Message | None:
        """Handle a "/command args" message.

        Returns a string to answer deterministically, a rewritten Message to
        run as a turn (/skill-name invocation), or None when the text isn't
        a command at all.
        """
        if not message.text.startswith("/"):
            return None
        name, _, args = message.text[1:].partition(" ")
        handler = self._commands.get(name)
        if handler is not None:
            return await handler(args.strip(), message)
        if self._skills is not None and (skill := self._skills.get(name)) is not None:
            text = (
                f"[skill invoked: /{name}]\n\n{skill.body()}\n\n"
                f"Arguments: {args.strip() or '(none)'}"
            )
            return replace(message, text=text)
        return f"unknown command /{name} — try /help"

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
