"""iMessage adapter (#156): poll-cursor behavior and the whitelist event gate.

The inbound central mechanism runs for real: every test executes the adapter's
actual SQL against a schema-true chat.db fixture (see ``imessage_helpers``) through
the production :meth:`ScriptRunner.run_sqlite` seam. Only the engine (the model
boundary) and the osascript child (the OS boundary) are faked.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.imessage import PLATFORM, IMessageAdapter, IMessageTaskIO
from chief.persistence import imessage as imessage_repo
from chief.persistence.contacts import get_contact
from chief.persistence.models import Contact, UnknownSender
from imessage_helpers import STYLE_GROUP, ChatDb, FakeEngine, FixtureRunner

OWNER = "+15550000001"
MOM = "+15550000002"
STRANGER = "+15550000003"


def _self_io(
    runner: FixtureRunner,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> IMessageTaskIO:
    """A self-DM IMessageTaskIO for the owner handle (prefixes + records sends)."""
    return IMessageTaskIO(
        runner,
        outbox_dir=str(tmp_path / "outbox"),
        self_dm=True,
        self_handles=frozenset({imessage_repo.normalize_handle(OWNER)}),
        session_factory=session_factory,
    )


def make_adapter(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    guest_enabled: bool = False,
    runner: FixtureRunner | None = None,
    self_dm: bool = False,
    io: IMessageTaskIO | None = None,
) -> tuple[IMessageAdapter, ChatDb, FixtureRunner, FakeEngine]:
    store = ChatDb(tmp_path / "chat.db")
    runner = runner or FixtureRunner()
    engine = FakeEngine()
    if io is None:
        io = (
            _self_io(runner, session_factory, tmp_path)
            if self_dm
            else IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))
        )
    adapter = IMessageAdapter(
        runner=runner,
        db_path=str(store.path),
        engine=engine,
        session_factory=session_factory,
        io=io,
        owner_handles=(OWNER,),
        guest_ack="Noted.",
        guest_enabled=guest_enabled,
        self_dm=self_dm,
    )
    return adapter, store, runner, engine


async def test_first_boot_initializes_cursor_to_store_head_and_replays_nothing(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    store.add_message(handle_rowid=handle, chat_rowid=chat, text="old history")

    await adapter.prime()
    processed = await adapter.poll_once()

    assert processed == 0
    assert engine.dispatched == []  # pre-existing history is never replayed


async def test_new_owner_message_dispatches_into_the_engine(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="hi chief")
    await adapter.poll_once()

    assert engine.dispatched == [(OWNER, "hi chief")]


async def test_cursor_persists_so_a_restart_neither_replays_nor_drops(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()
    store.add_message(handle_rowid=handle, chat_rowid=chat, text="first")
    await adapter.poll_once()

    # A message lands while the adapter is "down"; a fresh instance (same DB) must
    # pick it up exactly once and not re-dispatch "first".
    store.add_message(handle_rowid=handle, chat_rowid=chat, text="while down")
    restarted = IMessageAdapter(
        runner=runner,
        db_path=str(store.path),
        engine=engine,
        session_factory=session_factory,
        io=IMessageTaskIO(runner, outbox_dir=str(store.path.parent / "outbox")),
        owner_handles=(OWNER,),
        guest_ack="Noted.",
    )
    await restarted.prime()
    await restarted.poll_once()
    await restarted.poll_once()  # idempotent: nothing new on the second tick

    assert engine.dispatched == [(OWNER, "first"), (OWNER, "while down")]


async def test_stranger_text_burns_zero_tokens_and_logs_metadata_only(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(STRANGER)
    chat = store.add_chat(STRANGER)
    await adapter.prime()

    when = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    store.add_message(
        handle_rowid=handle, chat_rowid=chat, text="secret plans", when=when
    )
    await adapter.poll_once()

    assert engine.dispatched == []
    assert engine.dispatched_guests == []
    assert runner.jxa_calls == []  # no reply of any kind
    async with session_factory() as session:
        rows = list(
            (await session.execute(select(UnknownSender))).scalars()
        )
        # No contact row is minted for a stranger — the whitelist stays the gate.
        assert await get_contact(
            session, platform=PLATFORM, user_id=STRANGER
        ) is None
    assert len(rows) == 1
    assert rows[0].handle == STRANGER
    assert rows[0].count == 1
    # Metadata only: the row carries handle + timestamps, never content.
    assert not hasattr(rows[0], "text")


async def test_unknown_sender_log_accumulates_without_content(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, _ = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(STRANGER)
    chat = store.add_chat(STRANGER)
    await adapter.prime()
    base = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    store.add_message(handle_rowid=handle, chat_rowid=chat, text="one", when=base)
    store.add_message(
        handle_rowid=handle,
        chat_rowid=chat,
        text="two",
        when=base + timedelta(minutes=5),
    )

    await adapter.poll_once()

    async with session_factory() as session:
        unknown = await imessage_repo.list_unknown_senders(
            session, platform=PLATFORM
        )
    assert [(u.handle, u.count) for u in unknown] == [(STRANGER, 2)]
    assert unknown[0].last_seen > unknown[0].first_seen


async def test_whitelisted_guest_routes_through_the_guest_gate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(
        tmp_path, session_factory, guest_enabled=True
    )
    handle = store.add_handle(MOM)
    chat = store.add_chat(MOM)
    async with session_factory() as session:
        await imessage_repo.add_handle(session, handle=MOM, tier="guest")
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="dinner friday?")
    await adapter.poll_once()

    assert engine.dispatched == []
    assert engine.dispatched_guests == [(MOM, "dinner friday?", MOM)]


async def test_guest_with_guests_disabled_gets_the_canned_ack(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(
        tmp_path, session_factory, guest_enabled=False
    )
    handle = store.add_handle(MOM)
    chat = store.add_chat(MOM)
    async with session_factory() as session:
        await imessage_repo.add_handle(session, handle=MOM, tier="guest")
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="hello?")
    await adapter.poll_once()

    assert engine.dispatched_guests == []
    assert len(runner.jxa_calls) == 1  # the ack went out via osascript
    assert runner.jxa_calls[0][-2:] == (MOM, "Noted.")


async def test_group_thread_messages_are_never_read_or_answered(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)  # even the owner, in a group, is skipped
    group = store.add_chat(
        "chat123", style=STYLE_GROUP, room_name="chat123"
    )
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=group, text="group chatter")
    await adapter.poll_once()

    assert engine.dispatched == []
    assert engine.dispatched_guests == []
    assert runner.jxa_calls == []
    async with session_factory() as session:
        unknown = await imessage_repo.list_unknown_senders(
            session, platform=PLATFORM
        )
    assert unknown == []  # group traffic is not even metadata-logged


async def test_tapbacks_are_skipped(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_message(
        handle_rowid=handle,
        chat_rowid=chat,
        text="Loved “hi”",
        associated_message_type=2000,
    )
    await adapter.poll_once()

    assert engine.dispatched == []


async def test_owner_slash_command_dispatches_through_the_shared_registry(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="/tasks")
    await adapter.poll_once()

    assert engine.dispatched == []  # a command is not a turn
    assert len(runner.jxa_calls) == 1
    assert runner.jxa_calls[0][-2:] == (OWNER, "No active tasks.")


async def test_owner_handles_are_seeded_owner_tier_and_admitted(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, _, _, _ = make_adapter(tmp_path, session_factory)

    await adapter.prime()

    async with session_factory() as session:
        contact = await get_contact(session, platform=PLATFORM, user_id=OWNER)
        assert contact is not None
        assert contact.tier == "owner"
        assert contact.state == "admitted"
        pref = await imessage_repo.get_pref(session, OWNER)
        assert pref is not None and pref.contacted  # owner sends never re-card


async def test_poll_failure_is_loud_not_silent(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, _ = make_adapter(tmp_path, session_factory)
    await adapter.prime()
    store.path.unlink()  # the store vanishes (FDA revoked / path moved)

    with pytest.raises(RuntimeError):
        await adapter.poll_once()
    ok, detail = adapter.poll_status()
    assert not ok
    assert detail


async def test_blocked_whitelisted_guest_is_dropped_silently(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(
        tmp_path, session_factory, guest_enabled=True
    )
    handle = store.add_handle(MOM)
    chat = store.add_chat(MOM)
    async with session_factory() as session:
        contact = await imessage_repo.add_handle(
            session, handle=MOM, tier="guest"
        )
        contact.state = "blocked"
        await session.commit()
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="hello?")
    await adapter.poll_once()

    assert engine.dispatched_guests == []
    assert runner.jxa_calls == []


async def test_seeding_upgrades_an_existing_guest_contact_to_owner(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(session, handle=OWNER, tier="guest")
    adapter, _, _, _ = make_adapter(tmp_path, session_factory)

    await adapter.prime()

    async with session_factory() as session:
        contact = await get_contact(session, platform=PLATFORM, user_id=OWNER)
        assert contact is not None and contact.tier == "owner"
        rows = list((await session.execute(select(Contact))).scalars())
    assert len(rows) == 1  # upgraded in place, not duplicated


# ---- self-DM mode (#161) -----------------------------------------------------------


async def test_self_text_pair_dispatches_exactly_once(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(
        tmp_path, session_factory, self_dm=True
    )
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    # The observed self-chat pair: sent copy (filtered by is_from_me) + the
    # chat_id-NULL received copy (dispatched once).
    store.add_self_text(handle_rowid=handle, chat_rowid=chat, text="note to self")
    await adapter.poll_once()

    assert engine.dispatched == [(OWNER, "note to self")]


async def test_self_thread_slash_command_dispatches_and_reply_is_prefixed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, runner, engine = make_adapter(
        tmp_path, session_factory, self_dm=True
    )
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_self_text(handle_rowid=handle, chat_rowid=chat, text="/tasks")
    await adapter.poll_once()

    assert engine.dispatched == []  # a command is not a turn
    assert runner.jxa_calls[0][-2:] == (OWNER, "🤖 No active tasks.")


async def test_bot_prefixed_inbound_is_never_dispatched(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    adapter, store, _, engine = make_adapter(
        tmp_path, session_factory, self_dm=True
    )
    handle = store.add_handle(OWNER)
    await adapter.prime()

    # A "🤖 " row with no matching send-record: the stateless prefix skip catches it.
    store.add_message(handle_rowid=handle, chat_rowid=None, text="🤖 anything")
    await adapter.poll_once()

    assert engine.dispatched == []


async def test_recorded_send_is_skipped_across_restart(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner = FixtureRunner()
    io = _self_io(runner, session_factory, tmp_path)
    # Record chief's own sends (a text reply and a file), then a fresh adapter
    # instance (same session_factory + store) must skip their echoed store rows.
    await io.send(OWNER, "reply")  # records "🤖 reply"
    await io.send_file(OWNER, "chart.png", b"x")  # records "chart.png"

    store = ChatDb(tmp_path / "chat.db")
    handle = store.add_handle(OWNER)
    engine = FakeEngine()
    adapter = IMessageAdapter(
        runner=runner,
        db_path=str(store.path),
        engine=engine,
        session_factory=session_factory,
        io=io,
        owner_handles=(OWNER,),
        guest_ack="Noted.",
        self_dm=True,
    )
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=None, text="🤖 reply")
    store.add_message(handle_rowid=handle, chat_rowid=None, text="chart.png")
    await adapter.poll_once()

    assert engine.dispatched == []  # durable record survives the restart


async def test_loop_proof_round_trip(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner = FixtureRunner()
    io = _self_io(runner, session_factory, tmp_path)
    adapter, store, _, engine = make_adapter(
        tmp_path, session_factory, self_dm=True, runner=runner, io=io
    )
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_self_text(handle_rowid=handle, chat_rowid=chat, text="hi")
    await adapter.poll_once()
    assert engine.dispatched == [(OWNER, "hi")]

    # chief replies; its received-copy echo re-polls to zero further dispatch/sends.
    await io.send(OWNER, "answer")  # records "🤖 answer"
    sends_before = len(runner.jxa_calls)
    store.add_message(handle_rowid=handle, chat_rowid=None, text="🤖 answer")
    await adapter.poll_once()

    assert engine.dispatched == [(OWNER, "hi")]  # no re-dispatch
    assert len(runner.jxa_calls) == sends_before  # no further owner-directed send


async def test_self_dm_off_still_dispatches_a_bot_prefixed_owner_row(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # The flag strictly gates the new behavior: with self_dm off, a whitelisted
    # owner's "🤖 " row is an ordinary inbound and dispatches (dedicated-ID suite).
    adapter, store, _, engine = make_adapter(tmp_path, session_factory)
    handle = store.add_handle(OWNER)
    chat = store.add_chat(OWNER)
    await adapter.prime()

    store.add_message(handle_rowid=handle, chat_rowid=chat, text="🤖 hi")
    await adapter.poll_once()

    assert engine.dispatched == [(OWNER, "🤖 hi")]
