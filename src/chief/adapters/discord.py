"""Discord gateway adapter — the Telegram adapter's twin for a private server.

Mirrors :mod:`chief.adapters.telegram` so both platforms run at once off one DB: a text
channel is the casual inbox (the engine spawns a *thread* when a message warrants its
own task, matching Telegram's General-topic model), an in-channel thread *is* a task,
and the owner is the one id routed into the engine. Guests run through the shared M6
receptionist gate when ``guest_enabled``; otherwise they get the canned ack.

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
from dataclasses import replace
from io import BytesIO
from typing import Any, cast

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import ApprovalAction, ApprovalCard
from ..memory.store import OWNER_NAMESPACE
from ..persistence.contacts import get_or_create_contact
from .base import (
    BUDGET_BUTTON_LABELS,
    CALLBACK_PREFIX,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    Adapter,
    AdmissionAction,
    AdmissionCard,
    ApprovalResolver,
    Attachment,
    BudgetAction,
    BudgetCard,
    Engine,
    MemoryReader,
    Message,
    ReadyHook,
    ReplyFn,
    Surface,
    Tier,
    admission_payload,
    apply_admission,
    apply_budget_decision,
    budget_outcome_text,
    budget_payload,
    classify_tier,
    default_branch_title,
    handle_guest_message,
    is_engaged,
    is_supported_media,
    parse_admission,
    parse_budget,
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
    """Decode a ``thread_key`` into ``(channel_id, thread_id)``.

    A group key (``{channel}:grp`` for the owner session, ``{channel}:grp:guest`` for
    the receptionist) has a non-numeric thread component and no Discord thread — it maps
    to ``thread_id`` 0, so the send path posts to the channel itself.
    """
    channel_str, _, rest = thread_key.partition(":")
    try:
        thread_id = int(rest)
    except ValueError:
        thread_id = 0
    return int(channel_str), thread_id


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


def _admission_view(contact_id: int) -> discord.ui.View:
    """The two-button first-contact card (M6): admit the guest, or block them.

    Like the approval view, the decision rides in each button's ``custom_id``, so a tap
    resolves centrally in :meth:`DiscordAdapter.on_interaction` with no per-view state.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="✅ Admit",
            style=discord.ButtonStyle.success,
            custom_id=admission_payload(contact_id, AdmissionAction.ADMIT),
        )
    )
    view.add_item(
        discord.ui.Button(
            label="🚫 Block",
            style=discord.ButtonStyle.secondary,
            custom_id=admission_payload(contact_id, AdmissionAction.BLOCK),
        )
    )
    return view


#: Discord button style per budget choice (labels live in base.BUDGET_BUTTON_LABELS).
_BUDGET_STYLES = {
    BudgetAction.DOWNGRADE: discord.ButtonStyle.primary,
    BudgetAction.CONTINUE: discord.ButtonStyle.success,
    BudgetAction.OVERFLOW: discord.ButtonStyle.secondary,
}


def _budget_view(cycle: str) -> discord.ui.View:
    """The three-button budget choice card (M9): downgrade / continue / overflow.

    Like the other cards, each button's ``custom_id`` carries the decision (and the
    cycle), so :meth:`DiscordAdapter.on_interaction` resolves it with no per-view state.
    """
    view = discord.ui.View(timeout=None)
    for action in BudgetAction:
        view.add_item(
            discord.ui.Button(
                label=BUDGET_BUTTON_LABELS[action],
                style=_BUDGET_STYLES[action],
                custom_id=budget_payload(cycle, action),
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

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Upload ``data`` as a file to the thread (long-reply file output, M8)."""
        channel_id, thread_id = _parse(thread_key)
        target = cast(
            discord.abc.Messageable, await self._resolve(thread_id or channel_id)
        )
        file = discord.File(BytesIO(data), filename=filename)
        await target.send(content=caption, file=file)

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

    async def send_admission_card(self, route: str, card: AdmissionCard) -> None:
        """Post a first-contact admit/block card to ``route`` (M6, no ref tracked)."""
        channel_id, thread_id = _parse(route)
        target_id = thread_id or channel_id
        target = cast(discord.abc.Messageable, await self._resolve(target_id))
        await target.send(card.text, view=_admission_view(card.contact_id))

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Post the budget choice card to ``route`` (M9, no ref tracked)."""
        channel_id, thread_id = _parse(route)
        target_id = thread_id or channel_id
        target = cast(discord.abc.Messageable, await self._resolve(target_id))
        await target.send(card.text, view=_budget_view(card.cycle))


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
        io: DiscordTaskIO | None = None,
        guest_enabled: bool = False,
        front_desk_thread_key: str | None = None,
        guest_rate: int = 10,
        guest_rate_window: int = 3600,
        guest_global_rate: int = 60,
        group_chat_enabled: bool = False,
        owner_home_guild_id: int | None = None,
    ) -> None:
        self._client = client
        self._token = token
        self._engine = engine
        self._owner_id = owner_id
        self._guest_ack = guest_ack
        self._session_factory = session_factory
        self._approvals = approvals
        self._memory = memory
        self._io = io
        self._guest_enabled = guest_enabled
        self._front_desk_thread_key = front_desk_thread_key
        self._guest_rate = guest_rate
        self._guest_rate_window = guest_rate_window
        self._guest_global_rate = guest_global_rate
        # Group chats (M11): a guild other than the owner's home server is a GROUP
        # surface — read ambiently, answered only when @mentioned or replied-to.
        self._group_chat_enabled = group_chat_enabled
        self._owner_home_guild_id = owner_home_guild_id
        # Contacts already prompted this run — dedupe the admission card (see Telegram).
        self._prompted_admission: set[int] = set()
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
        """Normalize a Discord message into a :class:`Message`, or ``None`` to skip.

        A media-only message (image/PDF, no text) is kept so owner intake (M8) reaches
        the engine; the attachments themselves are read in :meth:`on_message`.
        """
        if not message.content and not message.attachments:
            return None
        surface, thread_key = self._surface(message)
        if thread_key is None:
            return None  # orphaned thread — no channel to route a key through
        author = message.author
        return Message(
            platform=PLATFORM,
            sender_id=author.id,
            text=message.content,
            thread_key=thread_key,
            tier=classify_tier(sender_id=author.id, owner_id=self._owner_id),
            sender_name=author.display_name,
            surface=surface,
        )

    def _surface(self, message: discord.Message) -> tuple[Surface, str | None]:
        """Classify the message's surface and pick its thread_key (M11).

        A guild other than the owner's home server, with group chats enabled, is a GROUP
        — one shared session keyed ``{channel}:grp`` (no Discord thread). A DM (no
        guild) and the home server keep today's thread/channel keying so behavior is
        untouched when group chats are off. ``None`` key = an orphaned thread to skip.
        """
        channel = message.channel
        guild = message.guild
        if (
            guild is not None
            and self._group_chat_enabled
            and guild.id != self._owner_home_guild_id
        ):
            return Surface.GROUP, f"{channel.id}:grp"
        surface = Surface.DM if guild is None else Surface.HOME
        if isinstance(channel, discord.Thread):
            if channel.parent_id is None:
                return surface, None
            return surface, f"{channel.parent_id}:{channel.id}"
        return surface, f"{channel.id}:0"

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
        if normalized.surface is Surface.GROUP:
            await self._on_group_message(message, normalized)
            return
        await self._record(normalized)
        if normalized.tier is not Tier.OWNER:
            logger.info("guest message", extra={"sender_id": normalized.sender_id})
            if not self._guest_enabled or self._io is None:
                await message.channel.send(self._guest_ack)
                return

            async def reply(text: str) -> None:
                await message.channel.send(text)

            await self._handle_guest(normalized, reply)
            return
        text = normalized.text
        if text.startswith("/"):
            await self._run_command(message, normalized, text)
            return
        attachments = await self._owner_attachments(message)
        if attachments:
            normalized = replace(normalized, attachments=attachments)
        is_general = not isinstance(message.channel, discord.Thread)
        logger.info("owner message", extra={"thread_key": normalized.thread_key})
        await self._engine.dispatch(
            thread_key=normalized.thread_key,
            text=text,
            attachments=normalized.attachments,
            is_general=is_general,
        )

    async def _on_group_message(
        self, raw: discord.Message, message: Message
    ) -> None:
        """Route a GROUP message: answer only when engaged, else read ambiently (M11).

        chief reads every group message as sender-attributed context but stays silent
        until @mentioned or replied-to. An engaged owner gets the full owner surface
        (flat, approvals DM'd); an engaged non-owner gets the receptionist — both keep
        the shared ``{channel}:grp`` session. A non-engaged message is buffered, silent.
        """
        if not self._group_engaged(raw):
            await self._engine.observe(
                thread_key=message.thread_key,
                text=message.text,
                sender_name=message.sender_name,
            )
            return
        await self._record(message)
        if message.tier is Tier.OWNER:
            attachments = await self._owner_attachments(raw)
            logger.info(
                "owner group message", extra={"thread_key": message.thread_key}
            )
            await self._engine.dispatch(
                thread_key=message.thread_key,
                text=message.text,
                attachments=attachments,
                surface=Surface.GROUP,
            )
            return
        logger.info("guest group message", extra={"thread_key": message.thread_key})
        await self._engine.dispatch_guest(
            thread_key=message.thread_key,
            text=message.text,
            from_label=message.sender_name,
            surface=Surface.GROUP,
        )

    def _group_engaged(self, message: discord.Message) -> bool:
        """True iff a group message addresses chief — an @mention or a reply to it."""
        me = self._client.user
        if me is None:
            return False
        mentioned = any(u.id == me.id for u in message.mentions)
        ref = message.reference
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        author = getattr(resolved, "author", None) if resolved is not None else None
        replied = author is not None and author.id == me.id
        return is_engaged(mentioned=mentioned, replied_to_bot=replied)

    @staticmethod
    async def _owner_attachments(
        message: discord.Message,
    ) -> tuple[Attachment, ...]:
        """Read the owner's image/PDF attachments (≤ caps); skip everything else (M8).

        Discord hands each attachment a ``content_type`` and ``size``; non-image/PDF or
        over-cap files are dropped silently, and the count is capped to bound memory.
        """
        items: list[Attachment] = []
        for att in message.attachments:
            content_type = att.content_type or ""
            if not is_supported_media(content_type) or att.size > MAX_ATTACHMENT_BYTES:
                continue
            data = await att.read()
            if len(data) > MAX_ATTACHMENT_BYTES:
                continue
            media_type = content_type.split(";")[0].strip()
            items.append(
                Attachment(media_type=media_type, data=data, filename=att.filename)
            )
            if len(items) >= MAX_ATTACHMENTS:
                break
        return tuple(items)

    async def _handle_guest(self, message: Message, reply: ReplyFn) -> None:
        """Run the shared guest gate (block / rate / mute / admit / dispatch, M6)."""
        assert self._io is not None  # narrowed by the caller's guest_enabled check
        if self._front_desk_thread_key is None:
            return  # never relay/admit with nowhere to route (config also guards this)
        await handle_guest_message(
            message=message,
            io=self._io,
            engine=self._engine,
            session_factory=self._session_factory,
            reply=reply,
            front_desk=self._front_desk_thread_key,
            guest_ack=self._guest_ack,
            rate_limit=self._guest_rate,
            rate_window_seconds=self._guest_rate_window,
            global_limit=self._guest_global_rate,
            prompted=self._prompted_admission,
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
            case "branch":
                # Only the casual inbox (``:0``) carries lossy context worth promoting;
                # a thread is already a tracked task. Optional arg names the new thread.
                if not message.thread_key.endswith(":0"):
                    await channel.send("/branch only works in the casual channel.")
                    return
                title = arg.strip() or default_branch_title()
                await self._engine.branch(message.thread_key, title)
                await channel.send(f'→ Branched into "{title}".')

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Resolve an approval, admission, or budget card button tap (owner only)."""
        if interaction.type is not discord.InteractionType.component:
            return
        data = cast(dict[str, Any], interaction.data or {})
        custom_id = data.get("custom_id")
        if not isinstance(custom_id, str):
            return
        approval = parse_callback(custom_id)
        admission = parse_admission(custom_id)
        budget = parse_budget(custom_id)
        if approval is None and admission is None and budget is None:
            return
        user = interaction.user
        if classify_tier(sender_id=user.id, owner_id=self._owner_id) is not Tier.OWNER:
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        if approval is not None and self._approvals is not None:
            approval_id, action = approval
            await self._approvals.resolve(approval_id, action, decided_by=str(user.id))
            await interaction.response.defer()
            return
        if admission is not None:
            await self._resolve_admission(interaction, *admission)
            return
        if budget is not None:
            await self._resolve_budget(interaction, *budget)

    async def _resolve_admission(
        self,
        interaction: discord.Interaction,
        contact_id: int,
        action: AdmissionAction,
    ) -> None:
        """Apply an admit/block tap and rewrite the card to its outcome (owner only)."""
        contact = await apply_admission(
            self._session_factory, contact_id=contact_id, action=action
        )
        self._prompted_admission.discard(contact_id)
        who = contact.display_name if contact and contact.display_name else "the guest"
        outcome = (
            f"✅ Admitted {who}."
            if action is AdmissionAction.ADMIT
            else f"🚫 Blocked {who}."
        )
        await interaction.response.edit_message(content=outcome, view=None)

    async def _resolve_budget(
        self,
        interaction: discord.Interaction,
        cycle: str,
        action: BudgetAction,
    ) -> None:
        """Apply a budget choice and rewrite the card to its outcome (owner only)."""
        await apply_budget_decision(
            self._session_factory, cycle=cycle, action=action
        )
        if action is BudgetAction.DOWNGRADE:
            # Flip live owner sessions now; new ones pick the model up via the mode.
            await self._engine.downgrade_live_sessions()
        await interaction.response.edit_message(
            content=budget_outcome_text(action), view=None
        )

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
