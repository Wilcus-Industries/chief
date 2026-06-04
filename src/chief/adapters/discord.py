"""Discord gateway adapter — the Telegram adapter's twin for a private server.

Mirrors :mod:`chief.adapters.telegram` so both platforms run at once off one DB: a text
channel is the casual inbox (the engine spawns a *thread* when a message warrants its
own task, matching Telegram's General-topic model), an in-channel thread *is* a task,
and the owner is the one id routed into the engine. Guests get the canned ack (full
receptionist scope is M6).

Outbound text flows back through :class:`DiscordTaskIO` (the engine's
:class:`~chief.core.tasks.TaskIO` *and* the gate's
:class:`~chief.gate.approvals.ApprovalIO`): ``thread_key`` is
``"{channel_id}:{thread_id}"`` (``thread_id`` 0 = the parent channel / casual inbox),
and create/archive map to Discord threads. Approval cards post a four-button
``discord.ui.View``; taps arrive as component interactions and resolve through the same
owner-gated path as Telegram's callback buttons.

Needs the privileged **message_content** intent (enable it in the Developer Portal) —
Discord otherwise delivers empty ``content`` and every message looks blank.
"""

import logging
from typing import Any, cast

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import ApprovalAction, ApprovalCard
from ..memory.store import OWNER_NAMESPACE
from ..persistence.contacts import get_or_create_contact
from .base import (
    CALLBACK_PREFIX,
    Adapter,
    ApprovalResolver,
    Engine,
    MemoryReader,
    Message,
    ReadyHook,
    Tier,
    classify_tier,
    parse_callback,
    split_message,
)

logger = logging.getLogger("chief.adapters.discord")

PLATFORM = "discord"
DISCORD_LIMIT = 2000  # per-message character cap
THREAD_NAME_LIMIT = 100

#: The four approval buttons: (label, action, style). The action value is baked into
#: each button's ``custom_id`` (``appr:{id}:{action}``), decoded by ``parse_callback``.
_BUTTONS: tuple[tuple[str, ApprovalAction, discord.ButtonStyle], ...] = (
    ("✅ Approve once", ApprovalAction.APPROVE_ONCE, discord.ButtonStyle.success),
    ("❌ Deny once", ApprovalAction.DENY_ONCE, discord.ButtonStyle.danger),
    ("⭐ Always allow", ApprovalAction.ALWAYS_ALLOW, discord.ButtonStyle.primary),
    ("🚫 Always deny", ApprovalAction.ALWAYS_DENY, discord.ButtonStyle.secondary),
)


def _parse(thread_key: str) -> tuple[int, int]:
    channel_str, thread_str = thread_key.split(":")
    return int(channel_str), int(thread_str)


def _approval_view(approval_id: int) -> discord.ui.View:
    """Build the four-button approval card view.

    The buttons carry the decision in their ``custom_id``; resolution happens centrally
    in :meth:`DiscordAdapter.on_interaction` (owner-gated, and restart-safe because the
    ``custom_id`` re-derives everything), so the buttons need no per-view callback.
    ``timeout=None`` keeps the card live until a tap or the gate's own timeout.
    """
    view = discord.ui.View(timeout=None)
    for label, action, style in _BUTTONS:
        view.add_item(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"{CALLBACK_PREFIX}:{approval_id}:{action.value}",
            )
        )
    return view


class DiscordTaskIO:
    """Engine → Discord output: send text and manage in-channel threads."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def _resolve(self, channel_id: int) -> object:
        """Return the channel/thread for ``channel_id`` (cache first, then a fetch)."""
        found = self._client.get_channel(channel_id)
        if found is not None:
            return found
        return await self._client.fetch_channel(channel_id)

    async def send(self, thread_key: str, text: str) -> None:
        channel_id, thread_id = _parse(thread_key)
        target = cast(
            discord.abc.Messageable, await self._resolve(thread_id or channel_id)
        )
        for chunk in split_message(text, DISCORD_LIMIT):
            await target.send(chunk)

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        channel_id, _ = _parse(like_thread_key)
        channel = cast(discord.TextChannel, await self._resolve(channel_id))
        thread = await channel.create_thread(name=title[:THREAD_NAME_LIMIT])
        return f"{channel_id}:{thread.id}"

    async def archive_thread(self, thread_key: str) -> None:
        _, thread_id = _parse(thread_key)
        if thread_id == 0:
            return  # the casual inbox channel has no thread to archive
        thread = cast(discord.Thread, await self._resolve(thread_id))
        await thread.edit(archived=True, locked=True)

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        """Post an approval card to ``route`` (a ``thread_key``); return its msg ref."""
        channel_id, thread_id = _parse(route)
        target_id = thread_id or channel_id
        target = cast(discord.abc.Messageable, await self._resolve(target_id))
        sent = await target.send(card.text, view=_approval_view(card.approval_id))
        return f"{target_id}:{sent.id}"

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Rewrite a posted card to its outcome, dropping the now-spent buttons."""
        channel_str, message_str = msg_ref.split(":")
        channel = cast(
            discord.abc.Messageable, await self._resolve(int(channel_str))
        )
        message = await channel.fetch_message(int(message_str))
        await message.edit(content=text, view=None)


class DiscordAdapter(Adapter):
    """Owner-aware Discord adapter that routes messages into the task engine."""

    def __init__(
        self,
        *,
        client: discord.Client,
        token: str,
        engine: Engine,
        owner_id: int,
        guest_ack: str,
        session_factory: async_sessionmaker[AsyncSession],
        approvals: ApprovalResolver | None = None,
        memory: MemoryReader | None = None,
    ) -> None:
        self._client = client
        self._token = token
        self._engine = engine
        self._owner_id = owner_id
        self._guest_ack = guest_ack
        self._session_factory = session_factory
        self._approvals = approvals
        self._memory = memory
        self._ready_hook: ReadyHook | None = None
        self._ready_fired = False
        self._register()

    def _register(self) -> None:
        # ``Client.event`` binds a coroutine as the handler for the event named after it
        # (``coro.__name__``); our methods are named to match the gateway events.
        self._client.event(self.on_message)
        self._client.event(self.on_interaction)
        self._client.event(self.on_ready)

    def to_message(self, message: discord.Message) -> Message | None:
        """Normalize a Discord message into a :class:`Message`, or ``None`` to skip."""
        if not message.content:
            return None
        channel = message.channel
        if isinstance(channel, discord.Thread):
            if channel.parent_id is None:
                return None  # orphaned thread — no channel to route a key through
            thread_key = f"{channel.parent_id}:{channel.id}"
        else:
            thread_key = f"{channel.id}:0"
        author = message.author
        return Message(
            platform=PLATFORM,
            sender_id=author.id,
            text=message.content,
            thread_key=thread_key,
            tier=classify_tier(sender_id=author.id, owner_id=self._owner_id),
            sender_name=author.display_name,
        )

    async def _record(self, message: Message) -> None:
        async with self._session_factory() as session:
            await get_or_create_contact(
                session,
                platform=message.platform,
                user_id=str(message.sender_id),
                tier=message.tier.value,
                display_name=message.sender_name,
            )

    async def on_message(self, message: discord.Message) -> None:
        me = self._client.user
        if me is not None and message.author.id == me.id:
            return  # ignore our own messages
        if message.author.bot:
            return  # ignore other bots
        normalized = self.to_message(message)
        if normalized is None:
            return
        await self._record(normalized)
        if normalized.tier is not Tier.OWNER:
            logger.info("guest message", extra={"sender_id": normalized.sender_id})
            await message.channel.send(self._guest_ack)
            return
        text = normalized.text
        if text.startswith("/"):
            await self._run_command(message, normalized, text)
            return
        is_general = not isinstance(message.channel, discord.Thread)
        logger.info("owner message", extra={"thread_key": normalized.thread_key})
        await self._engine.dispatch(
            thread_key=normalized.thread_key, text=text, is_general=is_general
        )

    async def _run_command(
        self, raw: discord.Message, message: Message, text: str
    ) -> None:
        """Run an owner ``/command`` (parsed inline — base Client has no router).

        The owner gate already passed in :meth:`on_message`; an unknown command is a
        silent no-op, matching Telegram (where ``MessageHandler`` ignores commands).
        """
        name, _, arg = text[1:].partition(" ")
        channel = raw.channel
        match name.lower():
            case "cancel":
                stopped = await self._engine.cancel(message.thread_key)
                await channel.send(
                    "Cancelled." if stopped else "Nothing running here."
                )
            case "tasks":
                tasks = await self._engine.active_tasks()
                if not tasks:
                    await channel.send("No active tasks.")
                else:
                    lines = [
                        f"• {t.title or t.thread_key} — {t.status}" for t in tasks
                    ]
                    await channel.send("\n".join(lines))
            case "memory":
                if self._memory is None:
                    await channel.send("Memory isn't enabled.")
                    return
                facts = self._memory.list_facts(OWNER_NAMESPACE)
                if not facts:
                    await channel.send("No memories yet.")
                else:
                    await channel.send("\n".join(f"• {f.title}" for f in facts))
            case "forget":
                if self._memory is None:
                    await channel.send("Memory isn't enabled.")
                    return
                query = arg.strip()
                if not query:
                    await channel.send("Usage: /forget <text>")
                    return
                removed = await self._memory.forget(OWNER_NAMESPACE, query)
                if not removed:
                    await channel.send("Nothing matched.")
                else:
                    await channel.send(
                        "Forgot: " + ", ".join(f.title for f in removed)
                    )

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Resolve an approval card button tap (owner only)."""
        if interaction.type is not discord.InteractionType.component:
            return
        data = cast(dict[str, Any], interaction.data or {})
        custom_id = data.get("custom_id")
        if not isinstance(custom_id, str):
            return
        parsed = parse_callback(custom_id)
        if parsed is None or self._approvals is None:
            return
        user = interaction.user
        if classify_tier(sender_id=user.id, owner_id=self._owner_id) is not Tier.OWNER:
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        approval_id, action = parsed
        await self._approvals.resolve(approval_id, action, decided_by=str(user.id))
        await interaction.response.defer()

    async def on_ready(self) -> None:
        """Fire the ready hook once (the gateway event re-fires on reconnect)."""
        if self._ready_fired:
            return
        self._ready_fired = True
        logger.info("discord adapter ready", extra={"user": str(self._client.user)})
        if self._ready_hook is not None:
            await self._ready_hook()

    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """Connect to the gateway and run until :meth:`stop` closes the client."""
        self._ready_hook = on_ready
        self._ready_fired = False
        logger.info("discord adapter starting (gateway)")
        await self._client.start(self._token)

    async def stop(self) -> None:
        """Signal :meth:`run` to shut the connection down."""
        await self._client.close()
