"""Discord adapter: routing into the engine, commands, and TaskIO output."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import AdmissionCard, Attachment, BudgetCard, Surface
from chief.adapters.commands import CommandContext, CommandRegistry, owner_registry
from chief.adapters.discord import DiscordAdapter, DiscordTaskIO
from chief.gate.approvals import ApprovalAction, ApprovalCard
from chief.memory.store import Fact
from chief.persistence import contacts as contact_repo
from chief.persistence import usage
from chief.persistence.contacts import get_or_create_contact
from chief.persistence.models import Contact, Task, Watch

OWNER_ID = 42
BOT_ID = 999


class FakeEngine:
    def __init__(
        self, *, active: list[Task] | None = None, cancel: bool = True
    ) -> None:
        self.dispatched: list[tuple[str, str, bool]] = []
        self.dispatched_attachments: list[tuple[Attachment, ...]] = []
        self.dispatched_surfaces: list[Any] = []
        self.dispatched_guests: list[tuple[str, str, str | None]] = []
        self.dispatched_guest_surfaces: list[Any] = []
        self.observed: list[tuple[str, str, str | None]] = []
        self.cancelled: list[str] = []
        self.branched: list[tuple[str, str]] = []
        self.escalated: list[str] = []
        self.reverted: list[str] = []
        self.routed: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.downgraded = 0
        self._active = active or []
        self._cancel = cancel

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
        surface: Any = None,
    ) -> None:
        self.dispatched.append((thread_key, text, is_general))
        self.dispatched_attachments.append(attachments)
        self.dispatched_surfaces.append(surface)

    async def dispatch_guest(
        self,
        *,
        thread_key: str,
        text: str,
        from_label: str | None = None,
        surface: Any = None,
    ) -> None:
        self.dispatched_guests.append((thread_key, text, from_label))
        self.dispatched_guest_surfaces.append(surface)

    async def observe(
        self, *, thread_key: str, text: str, sender_name: str | None = None
    ) -> None:
        self.observed.append((thread_key, text, sender_name))

    async def cancel(self, thread_key: str) -> bool:
        self.cancelled.append(thread_key)
        return self._cancel

    async def active_tasks(self) -> list[Task]:
        return self._active

    async def composed_skills(self) -> list[str]:
        return []

    async def branch(self, thread_key: str, title: str) -> str:
        self.branched.append((thread_key, title))
        return f"{thread_key.split(':')[0]}:88"

    async def escalate(self, thread_key: str) -> str:
        self.escalated.append(thread_key)
        return "⚡ Switched to Opus 4.8 for this thread — /sonnet to switch back."

    async def revert(self, thread_key: str) -> str:
        self.reverted.append(thread_key)
        return "↩️ Back to Sonnet 4.6 for this thread."

    async def route(self, thread_key: str, category: str) -> str:
        self.routed.append((thread_key, category))
        return f"🧭 Routing this thread as “{category}”."

    async def close(self, thread_key: str) -> str:
        self.closed.append(thread_key)
        return "Closed."

    async def rename(self, thread_key: str, title: str) -> str:
        self.renamed.append((thread_key, title))
        return f"Renamed to “{title}”."

    async def downgrade_live_sessions(self) -> None:
        self.downgraded += 1

    async def list_watches(self) -> list[Watch]:
        return []

    async def cancel_watch(self, watch_id: int) -> str:
        return f"Cancelled #{watch_id}."


class FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[int, ApprovalAction, str]] = []

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> bool:
        self.resolved.append((approval_id, action, decided_by))
        return True


def _fact(slug: str, title: str) -> Fact:
    return Fact(
        slug=slug,
        title=title,
        body="b",
        namespace="owner",
        provenance="inferred",
        trust="medium",
        expires=None,
        created="2026-06-03T00:00:00+00:00",
    )


class FakeMemory:
    def __init__(self, facts: list[Fact] | None = None) -> None:
        self._facts = facts or []
        self.forgot: list[tuple[str, str]] = []

    def list_facts(self, namespace: str) -> list[Fact]:
        return [f for f in self._facts if f.namespace == namespace]

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        self.forgot.append((namespace, query))
        removed = [f for f in self._facts if query.lower() in f.title.lower()]
        self._facts = [f for f in self._facts if f not in removed]
        return removed


def _client() -> Any:
    client = MagicMock()
    client.event = Mock()
    client.user = SimpleNamespace(id=BOT_ID)  # our own bot user (ignored on inbound)
    return client


class FakeIO:
    """Records Front Desk sends + admission cards (the guest gate's output channel)."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, str]] = []
        self.cards: list[tuple[str, AdmissionCard]] = []

    async def send(self, thread_key: str, text: str) -> None:
        self.sends.append((thread_key, text))

    async def send_admission_card(self, route: str, card: AdmissionCard) -> None:
        self.cards.append((route, card))


def _adapter(
    session_factory: async_sessionmaker[AsyncSession],
    engine: FakeEngine,
    *,
    approvals: FakeResolver | None = None,
    memory: FakeMemory | None = None,
    guest_enabled: bool = False,
    io: FakeIO | None = None,
    front_desk: str | None = "100:1",
    guest_rate: int = 10,
    group_chat_enabled: bool = False,
    owner_home_guild_id: int | None = None,
    commands: CommandRegistry | None = None,
) -> DiscordAdapter:
    return DiscordAdapter(
        client=cast(discord.Client, _client()),
        token="tok",
        engine=engine,
        owner_id=OWNER_ID,
        guest_ack="noted, thanks",
        session_factory=session_factory,
        approvals=approvals,
        memory=memory,
        io=cast(Any, io),
        guest_enabled=guest_enabled,
        front_desk_thread_key=front_desk,
        guest_rate=guest_rate,
        group_chat_enabled=group_chat_enabled,
        owner_home_guild_id=owner_home_guild_id,
        commands=commands,
    )


def _text_channel(channel_id: int = 100) -> Any:
    """A non-thread channel (the casual inbox) — ``isinstance(_, Thread)`` is False."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    return channel


def _thread(parent_id: int | None = 100, thread_id: int = 5) -> Any:
    """An in-channel thread (a task) — ``isinstance`` passes via the spec."""
    thread = MagicMock(spec=discord.Thread)
    thread.parent_id = parent_id
    thread.id = thread_id
    thread.send = AsyncMock()
    return thread


def _message(
    *,
    user_id: int,
    content: str,
    channel: Any,
    bot: bool = False,
    attachments: list[Any] | None = None,
    guild_id: int | None = None,
    mentions: list[Any] | None = None,
    reference: Any = None,
) -> discord.Message:
    message = SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=user_id, bot=bot, display_name="Someone"),
        channel=channel,
        attachments=attachments or [],
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        mentions=mentions or [],
        reference=reference,
    )
    return cast(discord.Message, message)


def _bot_mention() -> list[Any]:
    """A mentions list containing chief's own bot user (engagement via @mention)."""
    return [SimpleNamespace(id=BOT_ID)]


def _bot_reply_ref() -> Any:
    """A message.reference whose resolved message was authored by the bot."""
    return SimpleNamespace(
        resolved=SimpleNamespace(author=SimpleNamespace(id=BOT_ID))
    )


def _attachment(data: bytes, *, content_type: str, filename: str = "f") -> Any:
    """A discord.Attachment stand-in: ``read`` yields ``data`` (size from len)."""
    return SimpleNamespace(
        content_type=content_type,
        size=len(data),
        filename=filename,
        read=AsyncMock(return_value=data),
    )


def _interaction(
    *,
    user_id: int,
    custom_id: str,
    itype: discord.InteractionType = discord.InteractionType.component,
) -> discord.Interaction:
    interaction = SimpleNamespace(
        type=itype,
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
    )
    return cast(discord.Interaction, interaction)


async def _contacts(session_factory: async_sessionmaker[AsyncSession]) -> list[Contact]:
    async with session_factory() as session:
        return list((await session.execute(select(Contact))).scalars())


# ---- to_message normalization ------------------------------------------------


def test_to_message_thread_is_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(
        _message(user_id=OWNER_ID, content="hi", channel=_thread(100, 5))
    )

    assert message is not None
    assert message.tier.value == "owner"
    assert message.thread_key == "100:5"


def test_to_message_text_channel_is_inbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(
        _message(user_id=OWNER_ID, content="hi", channel=_text_channel(100))
    )

    assert message is not None
    assert message.thread_key == "100:0"


def test_to_message_ignores_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    assert (
        adapter.to_message(
            _message(user_id=OWNER_ID, content="", channel=_text_channel())
        )
        is None
    )


def test_to_message_ignores_thread_without_parent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An orphaned thread has no parent channel to key on — skip it, don't build a
    # "None:5" key that would later blow up in _parse's int().
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(
        _message(user_id=OWNER_ID, content="hi", channel=_thread(parent_id=None))
    )

    assert message is None


# ---- inbound routing ---------------------------------------------------------


async def test_owner_thread_message_dispatches_to_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="do it", channel=_thread(100, 5))
    )

    assert engine.dispatched == [("100:5", "do it", False)]
    contacts = await _contacts(session_factory)
    assert len(contacts) == 1 and contacts[0].tier == "owner"


async def test_owner_channel_message_flags_is_general(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="hey", channel=_text_channel(100))
    )

    assert engine.dispatched == [("100:0", "hey", True)]


# ---- group chats (M11) -------------------------------------------------------


def test_to_message_classifies_group_surface_and_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A non-home guild with group chats on is a GROUP, keyed {channel}:grp.
    adapter = _adapter(
        session_factory,
        FakeEngine(),
        group_chat_enabled=True,
        owner_home_guild_id=1,
    )

    message = adapter.to_message(
        _message(
            user_id=7, content="hi room", channel=_text_channel(200), guild_id=555
        )
    )

    assert message is not None
    assert message.surface is Surface.GROUP
    assert message.thread_key == "200:grp"


def test_to_message_home_guild_is_not_a_group(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The owner's own server is HOME, never a GROUP, even with groups enabled.
    adapter = _adapter(
        session_factory,
        FakeEngine(),
        group_chat_enabled=True,
        owner_home_guild_id=1,
    )

    message = adapter.to_message(
        _message(
            user_id=OWNER_ID, content="hey", channel=_text_channel(100), guild_id=1
        )
    )

    assert message is not None
    assert message.surface is Surface.HOME
    assert message.thread_key == "100:0"


async def test_group_message_without_mention_is_observed_not_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(
        session_factory, engine, group_chat_enabled=True, owner_home_guild_id=1
    )
    channel = _text_channel(200)

    await adapter.on_message(
        _message(user_id=7, content="just chatting", channel=channel, guild_id=555)
    )

    assert engine.observed == [("200:grp", "just chatting", "Someone")]
    assert engine.dispatched == [] and engine.dispatched_guests == []
    channel.send.assert_not_awaited()


async def test_group_owner_mention_dispatches_with_group_surface(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(
        session_factory, engine, group_chat_enabled=True, owner_home_guild_id=1
    )

    await adapter.on_message(
        _message(
            user_id=OWNER_ID,
            content="status?",
            channel=_text_channel(200),
            guild_id=555,
            mentions=_bot_mention(),
        )
    )

    assert engine.dispatched == [("200:grp", "status?", False)]
    assert engine.dispatched_surfaces == [Surface.GROUP]
    assert engine.observed == []


async def test_group_owner_reply_to_bot_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(
        session_factory, engine, group_chat_enabled=True, owner_home_guild_id=1
    )

    await adapter.on_message(
        _message(
            user_id=OWNER_ID,
            content="and the other thing?",
            channel=_text_channel(200),
            guild_id=555,
            reference=_bot_reply_ref(),
        )
    )

    assert engine.dispatched == [("200:grp", "and the other thing?", False)]
    assert engine.dispatched_surfaces == [Surface.GROUP]


async def test_group_guest_mention_dispatches_as_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(
        session_factory, engine, group_chat_enabled=True, owner_home_guild_id=1
    )

    await adapter.on_message(
        _message(
            user_id=7,
            content="who are you?",
            channel=_text_channel(200),
            guild_id=555,
            mentions=_bot_mention(),
        )
    )

    assert engine.dispatched_guests == [("200:grp", "who are you?", "Someone")]
    assert engine.dispatched_guest_surfaces == [Surface.GROUP]
    assert engine.dispatched == []


# ---- media intake (M8, owner only) -------------------------------------------


def test_to_message_keeps_attachment_only_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(
        _message(
            user_id=OWNER_ID,
            content="",
            channel=_text_channel(100),
            attachments=[_attachment(b"x", content_type="image/png")],
        )
    )

    assert message is not None and message.text == ""


async def test_owner_image_and_pdf_download(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _message(
        user_id=OWNER_ID,
        content="see these",
        channel=_text_channel(100),
        attachments=[
            _attachment(b"PNGDATA", content_type="image/png", filename="a.png"),
            _attachment(b"%PDF", content_type="application/pdf", filename="b.pdf"),
        ],
    )

    await adapter.on_message(update)

    (attachments,) = engine.dispatched_attachments
    assert attachments == (
        Attachment(media_type="image/png", data=b"PNGDATA", filename="a.png"),
        Attachment(media_type="application/pdf", data=b"%PDF", filename="b.pdf"),
    )


async def test_owner_unsupported_attachment_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _message(
        user_id=OWNER_ID,
        content="a zip",
        channel=_text_channel(100),
        attachments=[_attachment(b"PK", content_type="application/zip")],
    )

    await adapter.on_message(update)

    assert engine.dispatched_attachments == [()]


async def test_owner_attachments_capped_at_five(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _message(
        user_id=OWNER_ID,
        content="lots",
        channel=_text_channel(100),
        attachments=[
            _attachment(b"x", content_type="image/png") for _ in range(6)
        ],
    )

    await adapter.on_message(update)

    (attachments,) = engine.dispatched_attachments
    assert len(attachments) == 5  # MAX_ATTACHMENTS


async def test_guest_message_acks_without_dispatch_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=7, content="hello", channel=channel)
    )

    assert engine.dispatched == []
    channel.send.assert_awaited_once_with("noted, thanks")
    contacts = await _contacts(session_factory)
    assert contacts[0].tier == "guest"


async def test_guest_image_is_not_ingested(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Media is owner-only: a guest's image never reaches dispatch / read.
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(
            user_id=7,
            content="",
            channel=channel,
            attachments=[_attachment(b"x", content_type="image/png")],
        )
    )

    assert engine.dispatched == [] and engine.dispatched_attachments == []
    channel.send.assert_awaited_once_with("noted, thanks")


async def _set_state(
    session_factory: async_sessionmaker[AsyncSession], user_id: int, state: str
) -> int:
    async with session_factory() as session:
        contact = await get_or_create_contact(
            session,
            platform="discord",
            user_id=str(user_id),
            tier="guest",
            display_name="Someone",
        )
        await contact_repo.set_contact_state(session, contact, state)
        return contact.id


async def test_guest_first_contact_cards_owner_and_acks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    channel = _text_channel(7)

    await adapter.on_message(_message(user_id=7, content="hello?", channel=channel))

    assert engine.dispatched_guests == []
    assert any("hello?" in text for _, text in io.sends)
    assert len(io.cards) == 1
    channel.send.assert_awaited_once_with("noted, thanks")


async def test_guest_admitted_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_ADMITTED)
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=FakeIO())
    channel = _text_channel(7)

    await adapter.on_message(
        _message(user_id=7, content="free thursday?", channel=channel)
    )

    assert engine.dispatched_guests == [("7:0", "free thursday?", "Someone")]


async def test_guest_blocked_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_BLOCKED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    channel = _text_channel(7)

    await adapter.on_message(_message(user_id=7, content="spam", channel=channel))

    assert engine.dispatched_guests == [] and io.sends == [] and io.cards == []
    channel.send.assert_not_awaited()


async def test_guest_muted_relays_silently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_MUTED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    channel = _text_channel(7)

    await adapter.on_message(_message(user_id=7, content="still here", channel=channel))

    assert engine.dispatched_guests == []
    assert len(io.sends) == 1 and "still here" in io.sends[0][1]
    channel.send.assert_not_awaited()


async def test_admission_interaction_tap_admits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _set_state(session_factory, 7, contact_repo.STATE_PENDING)
    adapter = _adapter(session_factory, FakeEngine(), guest_enabled=True, io=FakeIO())
    interaction = _interaction(user_id=OWNER_ID, custom_id=f"adm:{contact_id}:admit")

    await adapter.on_interaction(interaction)

    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="discord", user_id="7"
        )
    assert contact is not None and contact.state == contact_repo.STATE_ADMITTED
    interaction.response.edit_message.assert_awaited_once()  # type: ignore[attr-defined]


async def test_admission_interaction_ignored_for_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _set_state(session_factory, 7, contact_repo.STATE_PENDING)
    adapter = _adapter(session_factory, FakeEngine(), guest_enabled=True, io=FakeIO())
    interaction = _interaction(user_id=7, custom_id=f"adm:{contact_id}:block")

    await adapter.on_interaction(interaction)

    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="discord", user_id="7"
        )
    assert contact is not None and contact.state == contact_repo.STATE_PENDING
    interaction.response.send_message.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Not allowed.", ephemeral=True
    )


async def test_budget_interaction_tap_flips_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())
    interaction = _interaction(user_id=OWNER_ID, custom_id="bud:2026-06:overflow")

    await adapter.on_interaction(interaction)

    async with session_factory() as session:
        row = await usage.get_row(session, "2026-06", usage.PREMIUM_REQUESTS)
    assert row is not None and row.mode == usage.MODE_OVERFLOW
    interaction.response.edit_message.assert_awaited_once()  # type: ignore[attr-defined]


async def test_budget_downgrade_tap_flips_live_sessions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    interaction = _interaction(user_id=OWNER_ID, custom_id="bud:2026-06:downgrade")

    await adapter.on_interaction(interaction)

    async with session_factory() as session:
        row = await usage.get_row(session, "2026-06", usage.PREMIUM_REQUESTS)
    # Downgrade moves the premium-request currency out of paused → turns resume (#97).
    assert row is not None and row.mode == usage.MODE_DOWNGRADED
    assert engine.downgraded == 1  # the tap flips live owner sessions too


async def test_budget_interaction_ignored_for_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())
    interaction = _interaction(user_id=7, custom_id="bud:2026-06:downgrade")

    await adapter.on_interaction(interaction)

    async with session_factory() as session:
        assert (
            await usage.get_row(session, "2026-06", usage.PREMIUM_REQUESTS) is None
        )
    interaction.response.send_message.assert_awaited_once_with(  # type: ignore[attr-defined]
        "Not allowed.", ephemeral=True
    )


async def test_bot_message_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=7, content="beep", channel=channel, bot=True)
    )

    assert engine.dispatched == []
    channel.send.assert_not_awaited()
    assert await _contacts(session_factory) == []


async def test_own_message_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter.on_message(
        _message(user_id=BOT_ID, content="my own output", channel=_text_channel())
    )

    assert engine.dispatched == []
    assert await _contacts(session_factory) == []


# ---- commands ----------------------------------------------------------------


async def test_cancel_owner_calls_engine_and_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(cancel=True)
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/cancel", channel=channel)
    )

    assert engine.cancelled == ["100:5"]
    channel.send.assert_awaited_once_with("Cancelled.")


async def test_cancel_nothing_running_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(cancel=False)
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/cancel", channel=channel)
    )

    channel.send.assert_awaited_once_with("Nothing running here.")


async def test_guest_command_is_acked_not_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=7, content="/cancel", channel=channel)
    )

    assert engine.cancelled == []
    channel.send.assert_awaited_once_with("noted, thanks")


async def test_tasks_lists_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task = Task(
        platform="discord", thread_key="100:5", tier="owner", status="running"
    )
    task.title = "big job"
    engine = FakeEngine(active=[task])
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/tasks", channel=channel)
    )

    channel.send.assert_awaited_once_with("• big job — running")


async def test_tasks_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(active=[])
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/tasks", channel=channel)
    )

    channel.send.assert_awaited_once_with("No active tasks.")


async def test_memory_lists_owner_facts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/memory", channel=channel)
    )

    channel.send.assert_awaited_once_with("• Prefers mornings")


async def test_memory_disabled_when_no_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())  # no memory
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/memory", channel=channel)
    )

    channel.send.assert_awaited_once_with("Memory isn't enabled.")


async def test_forget_removes_matching_fact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/forget mornings", channel=channel)
    )

    assert memory.forgot == [("owner", "mornings")]
    channel.send.assert_awaited_once_with("Forgot: Prefers mornings")


async def test_forget_no_argument_shows_usage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/forget", channel=channel)
    )

    assert memory.forgot == []
    channel.send.assert_awaited_once_with("Usage: /forget <text>")


async def test_forget_no_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/forget nonsense", channel=channel)
    )

    channel.send.assert_awaited_once_with("Nothing matched.")


# ---- /branch -----------------------------------------------------------------


async def test_branch_owner_casual_uses_default_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/branch", channel=channel)
    )

    assert len(engine.branched) == 1
    thread_key, title = engine.branched[0]
    assert thread_key == "100:0"
    assert title.startswith("Branched chat ")  # timestamped default
    channel.send.assert_awaited_once_with(f'→ Branched into "{title}".')


async def test_branch_owner_casual_uses_argument_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/branch Trip planning", channel=channel)
    )

    assert engine.branched == [("100:0", "Trip planning")]
    channel.send.assert_awaited_once_with('→ Branched into "Trip planning".')


async def test_branch_rejected_in_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/branch", channel=channel)
    )

    assert engine.branched == []  # a tracked thread is already full-memory
    channel.send.assert_awaited_once_with("/branch only works in the casual channel.")


# ---- /opus + /sonnet (M11) ---------------------------------------------------


async def test_opus_owner_escalates_and_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/opus", channel=channel)
    )

    assert engine.escalated == ["100:5"]
    channel.send.assert_awaited_once_with(
        "⚡ Switched to Opus 4.8 for this thread — /sonnet to switch back."
    )


async def test_sonnet_owner_reverts_and_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/sonnet", channel=channel)
    )

    assert engine.reverted == ["100:5"]
    channel.send.assert_awaited_once_with("↩️ Back to Sonnet 4.6 for this thread.")


async def test_opus_ignores_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A non-owner /opus never reaches _run_command — the guest gate upstream routes it
    # to the canned ack instead, so a guest can never escalate to Opus.
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _text_channel(100)

    await adapter.on_message(
        _message(user_id=7, content="/opus", channel=channel)
    )

    assert engine.escalated == []


async def test_route_owner_overrides_category_and_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/route code", channel=channel)
    )

    assert engine.routed == [("100:5", "code")]
    channel.send.assert_awaited_once_with("🧭 Routing this thread as “code”.")


async def test_route_without_argument_prints_usage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/route", channel=channel)
    )

    assert engine.routed == []  # no category → nothing routed
    channel.send.assert_awaited_once_with("Usage: /route <category>")


async def test_registry_new_command_served_discord(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A command registered once on the shared registry is served with no
    Discord-side change (#129)."""
    registry = owner_registry()

    async def _pong(ctx: CommandContext) -> None:
        await ctx.reply("pong")

    registry.register("ping", _pong)
    adapter = _adapter(session_factory, FakeEngine(), commands=registry)
    channel = _thread(100, 5)

    await adapter.on_message(
        _message(user_id=OWNER_ID, content="/ping", channel=channel)
    )

    channel.send.assert_awaited_once_with("pong")


# ---- DiscordTaskIO -----------------------------------------------------------


def _io_client(channel: Any) -> Any:
    client = MagicMock()
    client.get_channel.return_value = channel
    return client


async def test_taskio_send_targets_thread() -> None:
    channel = MagicMock()
    channel.send = AsyncMock()
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    await io.send("100:7", "hello")

    channel.send.assert_awaited_once_with("hello")


async def test_taskio_send_splits_long_text() -> None:
    channel = MagicMock()
    channel.send = AsyncMock()
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    await io.send("100:0", "a" * 5000)

    assert channel.send.await_count == 3  # 2000-char cap → 2000 + 2000 + 1000


async def test_taskio_send_group_key_posts_to_channel() -> None:
    # A GROUP session key ({channel}:grp, owner) and its {channel}:grp:guest variant
    # (group receptionist) both post to the channel — the non-numeric thread component
    # must resolve to the channel, not raise in _parse's int().
    channel = MagicMock()
    channel.send = AsyncMock()
    client = _io_client(channel)
    io = DiscordTaskIO(cast(discord.Client, client))

    await io.send("200:grp", "hi room")
    client.get_channel.assert_called_with(200)
    channel.send.assert_awaited_with("hi room")

    await io.send("200:grp:guest", "front desk here")
    client.get_channel.assert_called_with(200)
    channel.send.assert_awaited_with("front desk here")


async def test_taskio_send_file_sends_file() -> None:
    channel = MagicMock()
    channel.send = AsyncMock()
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    await io.send_file("100:0", "reply.md", b"data", caption="note")

    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "note"
    file = kwargs["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "reply.md"


async def test_taskio_create_thread_returns_key() -> None:
    channel = MagicMock()
    channel.create_thread = AsyncMock(return_value=SimpleNamespace(id=88))
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    key = await io.create_thread(like_thread_key="100:0", title="do a thing")

    assert key == "100:88"
    # Must be a PUBLIC thread — discord.py defaults create_thread to a *private*
    # thread the owner is never added to, so a spawned topic would be invisible.
    channel.create_thread.assert_awaited_once_with(
        name="do a thing", type=discord.ChannelType.public_thread
    )


async def test_taskio_archive_edits_thread() -> None:
    thread = MagicMock()
    thread.edit = AsyncMock()
    io = DiscordTaskIO(cast(discord.Client, _io_client(thread)))

    await io.archive_thread("100:7")

    thread.edit.assert_awaited_once_with(archived=True, locked=True)


async def test_taskio_archive_skips_inbox() -> None:
    client = MagicMock()
    io = DiscordTaskIO(cast(discord.Client, client))

    await io.archive_thread("100:0")

    client.get_channel.assert_not_called()


# ---- approval cards (ApprovalIO) ---------------------------------------------


async def test_send_card_posts_view_and_returns_ref() -> None:
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=77))
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    ref = await io.send_card("100:5", ApprovalCard(approval_id=9, text="Run: git push"))

    assert ref == "5:77"  # target id is the thread id (5), not the parent channel
    args, kwargs = channel.send.call_args
    assert args[0] == "Run: git push"
    view = kwargs["view"]
    payloads = [cast("discord.ui.Button[Any]", b).custom_id for b in view.children]
    assert payloads == [
        "appr:9:approve_once",
        "appr:9:deny_once",
        "appr:9:always_allow",
        "appr:9:always_deny",
    ]


async def test_edit_card_rewrites_outcome_and_drops_view() -> None:
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    await io.edit_card("100:77", "✅ Approved (once)")

    channel.fetch_message.assert_awaited_once_with(77)
    message.edit.assert_awaited_once_with(content="✅ Approved (once)", view=None)


async def test_send_budget_card_posts_three_choice_buttons() -> None:
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=77))
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    await io.send_budget_card(
        "100:5", BudgetCard(cycle="2026-06", text="🛑 Budget reached. Pick:")
    )

    args, kwargs = channel.send.call_args
    assert args[0] == "🛑 Budget reached. Pick:"
    payloads = [
        cast("discord.ui.Button[Any]", b).custom_id for b in kwargs["view"].children
    ]
    assert payloads == [
        "bud:2026-06:downgrade",
        "bud:2026-06:continue",
        "bud:2026-06:overflow",
    ]


# ---- on_interaction (approval button taps) -----------------------------------


async def test_interaction_owner_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    interaction = _interaction(user_id=OWNER_ID, custom_id="appr:9:approve_once")

    await adapter.on_interaction(interaction)

    assert resolver.resolved == [(9, ApprovalAction.APPROVE_ONCE, str(OWNER_ID))]
    cast(Any, interaction.response).defer.assert_awaited_once()


async def test_interaction_guest_denied(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    interaction = _interaction(user_id=7, custom_id="appr:9:approve_once")

    await adapter.on_interaction(interaction)

    assert resolver.resolved == []
    cast(Any, interaction.response).send_message.assert_awaited_once_with(
        "Not allowed.", ephemeral=True
    )


async def test_interaction_foreign_payload_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    interaction = _interaction(user_id=OWNER_ID, custom_id="other:9:approve_once")

    await adapter.on_interaction(interaction)

    assert resolver.resolved == []
    cast(Any, interaction.response).defer.assert_not_awaited()
    cast(Any, interaction.response).send_message.assert_not_awaited()


async def test_interaction_non_component_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    interaction = _interaction(
        user_id=OWNER_ID,
        custom_id="appr:9:approve_once",
        itype=discord.InteractionType.ping,
    )

    await adapter.on_interaction(interaction)

    assert resolver.resolved == []
