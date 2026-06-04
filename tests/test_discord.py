"""Discord adapter: routing into the engine, commands, and TaskIO output."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.discord import DiscordAdapter, DiscordTaskIO
from chief.gate.approvals import ApprovalAction, ApprovalCard
from chief.memory.store import Fact
from chief.persistence.models import Contact, Task

OWNER_ID = 42
BOT_ID = 999


class FakeEngine:
    def __init__(
        self, *, active: list[Task] | None = None, cancel: bool = True
    ) -> None:
        self.dispatched: list[tuple[str, str, bool]] = []
        self.cancelled: list[str] = []
        self.branched: list[tuple[str, str]] = []
        self._active = active or []
        self._cancel = cancel

    async def dispatch(
        self, *, thread_key: str, text: str, is_general: bool = False
    ) -> None:
        self.dispatched.append((thread_key, text, is_general))

    async def cancel(self, thread_key: str) -> bool:
        self.cancelled.append(thread_key)
        return self._cancel

    async def active_tasks(self) -> list[Task]:
        return self._active

    async def branch(self, thread_key: str, title: str) -> str:
        self.branched.append((thread_key, title))
        return f"{thread_key.split(':')[0]}:88"


class FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[int, ApprovalAction, str]] = []

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> None:
        self.resolved.append((approval_id, action, decided_by))


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


def _adapter(
    session_factory: async_sessionmaker[AsyncSession],
    engine: FakeEngine,
    *,
    approvals: FakeResolver | None = None,
    memory: FakeMemory | None = None,
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
    *, user_id: int, content: str, channel: Any, bot: bool = False
) -> discord.Message:
    message = SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=user_id, bot=bot, display_name="Someone"),
        channel=channel,
    )
    return cast(discord.Message, message)


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
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
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


async def test_guest_message_acks_without_dispatch(
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


async def test_taskio_create_thread_returns_key() -> None:
    channel = MagicMock()
    channel.create_thread = AsyncMock(return_value=SimpleNamespace(id=88))
    io = DiscordTaskIO(cast(discord.Client, _io_client(channel)))

    key = await io.create_thread(like_thread_key="100:0", title="do a thing")

    assert key == "100:88"
    channel.create_thread.assert_awaited_once_with(name="do a thing")


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
