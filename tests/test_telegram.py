"""Telegram adapter: routing into the engine, commands, and TaskIO output."""

import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import Application

from chief.adapters.base import (
    AdmissionCard,
    Attachment,
    BudgetCard,
    parse_callback,
)
from chief.adapters.telegram import (
    CALLBACK_QUERY_PATTERN,
    TelegramAdapter,
    TelegramTaskIO,
    split_message,
)
from chief.gate.approvals import ApprovalAction, ApprovalCard
from chief.memory.store import Fact
from chief.persistence import contacts as contact_repo
from chief.persistence import usage
from chief.persistence.contacts import get_or_create_contact
from chief.persistence.models import Contact, Task

OWNER_ID = 42
_CTX = cast(Any, SimpleNamespace())


class FakeEngine:
    def __init__(
        self, *, active: list[Task] | None = None, cancel: bool = True
    ) -> None:
        self.dispatched: list[tuple[str, str, bool]] = []
        self.dispatched_attachments: list[tuple[Attachment, ...]] = []
        self.dispatched_guests: list[tuple[str, str, str | None]] = []
        self.cancelled: list[str] = []
        self.branched: list[tuple[str, str]] = []
        self._active = active or []
        self._cancel = cancel

    async def dispatch(
        self,
        *,
        thread_key: str,
        text: str,
        attachments: tuple[Attachment, ...] = (),
        is_general: bool = False,
    ) -> None:
        self.dispatched.append((thread_key, text, is_general))
        self.dispatched_attachments.append(attachments)

    async def dispatch_guest(
        self, *, thread_key: str, text: str, from_label: str | None = None
    ) -> None:
        self.dispatched_guests.append((thread_key, text, from_label))

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
    front_desk: str | None = "-100:1",
    guest_rate: int = 10,
    guest_global_rate: int = 60,
) -> TelegramAdapter:
    app = cast(Application, SimpleNamespace(add_handler=Mock()))  # type: ignore[type-arg]
    return TelegramAdapter(
        application=app,
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
        guest_global_rate=guest_global_rate,
    )


def _callback_update(*, user_id: int, data: str) -> Update:
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock()
        ),
        effective_user=SimpleNamespace(id=user_id, full_name="Someone"),
    )
    return cast(Update, update)


def _fake_update(
    *,
    user_id: int,
    text: str | None,
    thread_id: int | None = None,
    is_forum: bool = False,
    chat_id: int = -100,
    caption: str | None = None,
    photo: list[Any] | None = None,
    document: Any | None = None,
) -> Update:
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            text=text,
            message_thread_id=thread_id,
            reply_text=AsyncMock(),
            caption=caption,
            photo=photo or [],
            document=document,
        ),
        effective_user=SimpleNamespace(id=user_id, full_name="Someone"),
        effective_chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )
    return cast(Update, update)


def _tg_file(data: bytes) -> Any:
    """A Telegram file handle whose ``download_as_bytearray`` yields ``data``."""
    return SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(data))
    )


def _photo(data: bytes, *, file_size: int | None = None) -> Any:
    """A PhotoSize stand-in: ``get_file`` returns a handle over ``data``."""
    return SimpleNamespace(
        file_size=file_size if file_size is not None else len(data),
        get_file=AsyncMock(return_value=_tg_file(data)),
    )


def _document(data: bytes, *, mime_type: str, file_name: str = "f") -> Any:
    """A Telegram Document stand-in (PDF/image) over ``data``."""
    return SimpleNamespace(
        mime_type=mime_type,
        file_name=file_name,
        file_size=len(data),
        get_file=AsyncMock(return_value=_tg_file(data)),
    )


async def _contacts(session_factory: async_sessionmaker[AsyncSession]) -> list[Contact]:
    async with session_factory() as session:
        return list((await session.execute(select(Contact))).scalars())


# ---- to_message normalization ------------------------------------------------


def test_to_message_classifies_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(_fake_update(user_id=OWNER_ID, text="hi", thread_id=5))

    assert message is not None
    assert message.tier.value == "owner"
    assert message.thread_key == "-100:5"


def test_to_message_ignores_non_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())

    assert adapter.to_message(_fake_update(user_id=OWNER_ID, text=None)) is None


def test_to_message_keeps_media_only_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A bare photo (no text, no caption) is still a message — _on_message reads its
    # bytes. The caption, when present, becomes the text.
    adapter = _adapter(session_factory, FakeEngine())

    message = adapter.to_message(
        _fake_update(user_id=OWNER_ID, text=None, photo=[_photo(b"x")])
    )

    assert message is not None and message.text == ""


# ---- media intake (M8, owner only) -------------------------------------------


async def test_owner_photo_downloads_attachment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID, text=None, caption="look", photo=[_photo(b"JPEGBYTES")]
    )

    await adapter._on_message(update, _CTX)

    assert engine.dispatched == [("-100:0", "look", False)]
    (attachments,) = engine.dispatched_attachments
    assert attachments == (
        Attachment(media_type="image/jpeg", data=b"JPEGBYTES", filename=None),
    )


async def test_owner_pdf_document_downloads_attachment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID,
        text="read this",
        document=_document(b"%PDF-1.7", mime_type="application/pdf", file_name="r.pdf"),
    )

    await adapter._on_message(update, _CTX)

    (attachments,) = engine.dispatched_attachments
    assert attachments == (
        Attachment(media_type="application/pdf", data=b"%PDF-1.7", filename="r.pdf"),
    )


async def test_owner_unsupported_document_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID,
        text="a zip",
        document=_document(b"PK\x03\x04", mime_type="application/zip"),
    )

    await adapter._on_message(update, _CTX)

    assert engine.dispatched_attachments == [()]  # non-image/PDF dropped


async def test_owner_oversized_photo_is_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from chief.adapters.base import MAX_ATTACHMENT_BYTES

    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID,
        text="huge",
        photo=[_photo(b"x", file_size=MAX_ATTACHMENT_BYTES + 1)],
    )

    await adapter._on_message(update, _CTX)

    assert engine.dispatched_attachments == [()]  # over the 20 MB cap → dropped


async def test_guest_photo_is_not_ingested(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Media is owner-only: a guest's photo never reaches dispatch / download.
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=7, text=None, photo=[_photo(b"JPEGBYTES")])

    await adapter._on_message(update, _CTX)

    assert engine.dispatched == [] and engine.dispatched_attachments == []
    update.effective_message.reply_text.assert_awaited_once_with("noted, thanks")  # type: ignore[union-attr]


# ---- inbound routing ---------------------------------------------------------


async def test_owner_task_message_dispatches_to_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter._on_message(
        _fake_update(user_id=OWNER_ID, text="do it", thread_id=5, is_forum=True), _CTX
    )

    assert engine.dispatched == [("-100:5", "do it", False)]
    contacts = await _contacts(session_factory)
    assert len(contacts) == 1 and contacts[0].tier == "owner"


async def test_owner_general_message_flags_is_general(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter._on_message(
        _fake_update(user_id=OWNER_ID, text="hey", thread_id=None, is_forum=True), _CTX
    )

    assert engine.dispatched == [("-100:0", "hey", True)]


async def test_flat_dm_is_not_general(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)

    await adapter._on_message(
        _fake_update(user_id=OWNER_ID, text="hi", thread_id=None, is_forum=False), _CTX
    )

    assert engine.dispatched == [("-100:0", "hi", False)]


async def test_guest_message_acks_without_dispatch_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # guest_enabled=False (default) preserves the pre-M6 canned-ack behavior.
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=7, text="hello", thread_id=None)

    await adapter._on_message(update, _CTX)

    assert engine.dispatched == []
    update.effective_message.reply_text.assert_awaited_once_with("noted, thanks")  # type: ignore[union-attr]
    contacts = await _contacts(session_factory)
    assert contacts[0].tier == "guest"


async def _set_state(
    session_factory: async_sessionmaker[AsyncSession], user_id: int, state: str
) -> int:
    async with session_factory() as session:
        contact = await get_or_create_contact(
            session,
            platform="telegram",
            user_id=str(user_id),
            tier="guest",
            display_name="Someone",
        )
        await contact_repo.set_contact_state(session, contact, state)
        return contact.id


async def test_guest_first_contact_cards_owner_relays_and_acks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    update = _fake_update(user_id=7, text="hello?", thread_id=None, chat_id=7)

    await adapter._on_message(update, _CTX)

    # Pending first contact: relay to Front Desk + admission card + holding ack, no
    # dispatch into a full session yet.
    assert engine.dispatched_guests == []
    assert any("-100:1" == route and "hello?" in text for route, text in io.sends)
    assert len(io.cards) == 1 and io.cards[0][0] == "-100:1"
    update.effective_message.reply_text.assert_awaited_once_with("noted, thanks")  # type: ignore[union-attr]


async def test_guest_admitted_dispatches_to_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_ADMITTED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    update = _fake_update(
        user_id=7, text="are you free thursday?", thread_id=None, chat_id=7
    )

    await adapter._on_message(update, _CTX)

    assert engine.dispatched_guests == [("7:0", "are you free thursday?", "Someone")]
    assert io.cards == []  # no admission card for an admitted guest


async def test_guest_blocked_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_BLOCKED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    update = _fake_update(user_id=7, text="spam", thread_id=None, chat_id=7)

    await adapter._on_message(update, _CTX)

    assert engine.dispatched_guests == [] and io.sends == [] and io.cards == []
    update.effective_message.reply_text.assert_not_awaited()  # type: ignore[union-attr]


async def test_guest_muted_relays_silently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_MUTED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io)
    update = _fake_update(user_id=7, text="still here", thread_id=None, chat_id=7)

    await adapter._on_message(update, _CTX)

    assert engine.dispatched_guests == []
    assert len(io.sends) == 1 and "still here" in io.sends[0][1]
    assert io.cards == []  # muted: no card
    update.effective_message.reply_text.assert_not_awaited()  # type: ignore[union-attr]


async def test_guest_over_rate_limit_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_state(session_factory, 7, contact_repo.STATE_ADMITTED)
    engine = FakeEngine()
    io = FakeIO()
    adapter = _adapter(session_factory, engine, guest_enabled=True, io=io, guest_rate=2)

    for _ in range(3):
        await adapter._on_message(
            _fake_update(user_id=7, text="hi", thread_id=None, chat_id=7), _CTX
        )

    # Cap is 2 → only the first two dispatch; the third is dropped silently.
    assert len(engine.dispatched_guests) == 2


def test_callback_handler_pattern_covers_both_card_kinds() -> None:
    # The registered CallbackQueryHandler only dispatches payloads matching this regex;
    # it MUST cover approval (appr:), admission (adm:), and budget (bud:) cards, or the
    # owner's taps are silently dropped before reaching _on_callback.
    assert re.match(CALLBACK_QUERY_PATTERN, "appr:9:approve_once")
    assert re.match(CALLBACK_QUERY_PATTERN, "adm:5:admit")
    assert re.match(CALLBACK_QUERY_PATTERN, "adm:5:block")
    assert re.match(CALLBACK_QUERY_PATTERN, "bud:2026-06:downgrade")
    assert not re.match(CALLBACK_QUERY_PATTERN, "other:1:x")


async def test_budget_card_tap_flips_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())
    update = _callback_update(user_id=OWNER_ID, data="bud:2026-06:downgrade")

    await adapter._on_callback(update, _CTX)

    async with session_factory() as session:
        row = await usage.get_row(session, "2026-06")
    assert row is not None and row.mode == usage.MODE_DOWNGRADED
    update.callback_query.edit_message_text.assert_awaited_once()  # type: ignore[union-attr]


async def test_budget_card_tap_ignored_for_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine())
    update = _callback_update(user_id=7, data="bud:2026-06:continue")

    await adapter._on_callback(update, _CTX)

    async with session_factory() as session:
        assert await usage.get_row(session, "2026-06") is None
    update.callback_query.answer.assert_awaited_once_with("Not allowed.")  # type: ignore[union-attr]


async def test_admission_card_tap_admits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _set_state(session_factory, 7, contact_repo.STATE_PENDING)
    adapter = _adapter(session_factory, FakeEngine(), guest_enabled=True, io=FakeIO())
    update = _callback_update(user_id=OWNER_ID, data=f"adm:{contact_id}:admit")

    await adapter._on_callback(update, _CTX)

    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="telegram", user_id="7"
        )
    assert contact is not None and contact.state == contact_repo.STATE_ADMITTED
    update.callback_query.edit_message_text.assert_awaited_once()  # type: ignore[union-attr]


async def test_admission_card_tap_ignored_for_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _set_state(session_factory, 7, contact_repo.STATE_PENDING)
    adapter = _adapter(session_factory, FakeEngine(), guest_enabled=True, io=FakeIO())
    update = _callback_update(user_id=7, data=f"adm:{contact_id}:admit")

    await adapter._on_callback(update, _CTX)

    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="telegram", user_id="7"
        )
    assert contact is not None and contact.state == contact_repo.STATE_PENDING
    update.callback_query.answer.assert_awaited_once_with("Not allowed.")  # type: ignore[union-attr]


# ---- commands ----------------------------------------------------------------


async def test_cancel_owner_calls_engine_and_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(cancel=True)
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=OWNER_ID, text="/cancel", thread_id=5)

    await adapter._on_cancel(update, _CTX)

    assert engine.cancelled == ["-100:5"]
    update.effective_message.reply_text.assert_awaited_once_with("Cancelled.")  # type: ignore[union-attr]


async def test_cancel_nothing_running_replies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(cancel=False)
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=OWNER_ID, text="/cancel", thread_id=5)

    await adapter._on_cancel(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "Nothing running here."
    )


async def test_cancel_ignores_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=7, text="/cancel", thread_id=5)

    await adapter._on_cancel(update, _CTX)

    assert engine.cancelled == []
    update.effective_message.reply_text.assert_not_awaited()  # type: ignore[union-attr]


async def test_tasks_lists_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task = Task(
        platform="telegram", thread_key="-100:5", tier="owner", status="running"
    )
    task.title = "big job"
    engine = FakeEngine(active=[task])
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=OWNER_ID, text="/tasks", thread_id=0)

    await adapter._on_tasks(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "• big job — running"
    )


async def test_tasks_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine(active=[])
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=OWNER_ID, text="/tasks", thread_id=0)

    await adapter._on_tasks(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with("No active tasks.")  # type: ignore[union-attr]


async def test_memory_lists_owner_facts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    update = _fake_update(user_id=OWNER_ID, text="/memory", thread_id=0)

    await adapter._on_memory(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "• Prefers mornings"
    )


async def test_memory_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, FakeEngine(), memory=FakeMemory())
    update = _fake_update(user_id=OWNER_ID, text="/memory", thread_id=0)

    await adapter._on_memory(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with("No memories yet.")  # type: ignore[union-attr]


async def test_memory_ignores_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    update = _fake_update(user_id=7, text="/memory", thread_id=0)

    await adapter._on_memory(update, _CTX)

    update.effective_message.reply_text.assert_not_awaited()  # type: ignore[union-attr]


async def test_forget_removes_matching_fact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    update = _fake_update(user_id=OWNER_ID, text="/forget mornings", thread_id=0)

    await adapter._on_forget(update, _CTX)

    assert memory.forgot == [("owner", "mornings")]
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "Forgot: Prefers mornings"
    )


async def test_forget_no_argument_shows_usage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    update = _fake_update(user_id=OWNER_ID, text="/forget", thread_id=0)

    await adapter._on_forget(update, _CTX)

    assert memory.forgot == []
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "Usage: /forget <text>"
    )


async def test_forget_no_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    memory = FakeMemory([_fact("mornings", "Prefers mornings")])
    adapter = _adapter(session_factory, FakeEngine(), memory=memory)
    update = _fake_update(user_id=OWNER_ID, text="/forget nonsense", thread_id=0)

    await adapter._on_forget(update, _CTX)

    update.effective_message.reply_text.assert_awaited_once_with("Nothing matched.")  # type: ignore[union-attr]


# ---- /branch -----------------------------------------------------------------


async def test_branch_owner_casual_uses_default_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID, text="/branch", thread_id=0, is_forum=True
    )

    await adapter._on_branch(update, _CTX)

    assert len(engine.branched) == 1
    thread_key, title = engine.branched[0]
    assert thread_key == "-100:0"
    assert title.startswith("Branched chat ")  # timestamped default
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        f'→ Branched into "{title}".'
    )


async def test_branch_owner_casual_uses_argument_title(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID, text="/branch Trip planning", thread_id=0, is_forum=True
    )

    await adapter._on_branch(update, _CTX)

    assert engine.branched == [("-100:0", "Trip planning")]
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        '→ Branched into "Trip planning".'
    )


async def test_branch_rejected_in_task_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID, text="/branch", thread_id=5, is_forum=True
    )

    await adapter._on_branch(update, _CTX)

    assert engine.branched == []  # a tracked topic is already full-memory
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "/branch only works in the casual channel."
    )


async def test_branch_rejected_in_flat_dm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A non-forum flat DM keys to :0 but runs as a normal archiving task, not the
    # self-compacting casual lane — so /branch must reject it, matching dispatch's
    # is_forum AND :0 predicate for is_general.
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(
        user_id=OWNER_ID, text="/branch", thread_id=0, is_forum=False
    )

    await adapter._on_branch(update, _CTX)

    assert engine.branched == []
    update.effective_message.reply_text.assert_awaited_once_with(  # type: ignore[union-attr]
        "/branch only works in the casual channel."
    )


async def test_branch_ignores_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=7, text="/branch", thread_id=0)

    await adapter._on_branch(update, _CTX)

    assert engine.branched == []
    update.effective_message.reply_text.assert_not_awaited()  # type: ignore[union-attr]


# ---- TelegramTaskIO ----------------------------------------------------------


async def test_taskio_send_targets_topic() -> None:
    bot = AsyncMock()
    await TelegramTaskIO(bot).send("-100:7", "hello")
    bot.send_message.assert_awaited_once_with(
        chat_id=-100, text="hello", message_thread_id=7
    )


async def test_taskio_send_general_has_no_thread() -> None:
    bot = AsyncMock()
    await TelegramTaskIO(bot).send("-100:0", "hi")
    bot.send_message.assert_awaited_once_with(
        chat_id=-100, text="hi", message_thread_id=None
    )


async def test_taskio_send_splits_long_text() -> None:
    bot = AsyncMock()
    await TelegramTaskIO(bot).send("-100:0", "a" * 9000)
    assert bot.send_message.await_count == 3


async def test_taskio_send_file_uploads_document() -> None:
    bot = AsyncMock()

    await TelegramTaskIO(bot).send_file("-100:7", "reply.md", b"data", caption="note")

    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["filename"] == "reply.md"
    assert kwargs["caption"] == "note"
    assert kwargs["message_thread_id"] == 7
    assert kwargs["document"].read() == b"data"


async def test_taskio_create_thread_returns_key() -> None:
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=88)
    key = await TelegramTaskIO(bot).create_thread(
        like_thread_key="-100:0", title="do a thing"
    )
    assert key == "-100:88"
    bot.create_forum_topic.assert_awaited_once_with(chat_id=-100, name="do a thing")


async def test_taskio_archive_closes_topic() -> None:
    bot = AsyncMock()
    await TelegramTaskIO(bot).archive_thread("-100:7")
    bot.close_forum_topic.assert_awaited_once_with(chat_id=-100, message_thread_id=7)


async def test_taskio_archive_skips_general() -> None:
    bot = AsyncMock()
    await TelegramTaskIO(bot).archive_thread("-100:0")
    bot.close_forum_topic.assert_not_awaited()


# ---- split_message -----------------------------------------------------------


def test_split_message_under_limit_single() -> None:
    assert split_message("hi") == ["hi"]


def test_split_message_splits_over_limit() -> None:
    chunks = split_message("a" * 9000)
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == "a" * 9000


# ---- approval cards (ApprovalIO) ---------------------------------------------


async def test_send_card_posts_keyboard_and_returns_ref() -> None:
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)

    ref = await TelegramTaskIO(bot).send_card(
        "-100:5", ApprovalCard(approval_id=9, text="Run: git push")
    )

    assert ref == "-100:77"
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["text"] == "Run: git push"
    assert kwargs["message_thread_id"] == 5
    # All four buttons, payloads carrying the approval id + action token.
    rows = kwargs["reply_markup"].inline_keyboard
    payloads = [b.callback_data for row in rows for b in row]
    assert payloads == [
        "appr:9:approve_once",
        "appr:9:deny_once",
        "appr:9:always_allow",
        "appr:9:always_deny",
    ]


async def test_edit_card_rewrites_outcome() -> None:
    bot = AsyncMock()

    await TelegramTaskIO(bot).edit_card("-100:77", "✅ Approved (once)")

    bot.edit_message_text.assert_awaited_once_with(
        text="✅ Approved (once)", chat_id=-100, message_id=77
    )


async def test_send_budget_card_posts_three_choice_buttons() -> None:
    bot = AsyncMock()

    await TelegramTaskIO(bot).send_budget_card(
        "-100:5", BudgetCard(cycle="2026-06", text="🛑 Budget reached. Pick:")
    )

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["message_thread_id"] == 5
    rows = kwargs["reply_markup"].inline_keyboard
    payloads = [b.callback_data for row in rows for b in row]
    assert payloads == [
        "bud:2026-06:downgrade",
        "bud:2026-06:continue",
        "bud:2026-06:overflow",
    ]


# ---- parse_callback ----------------------------------------------------------


def test_parse_callback_valid() -> None:
    assert parse_callback("appr:9:always_allow") == (9, ApprovalAction.ALWAYS_ALLOW)


def test_parse_callback_rejects_foreign_or_malformed() -> None:
    assert parse_callback("other:9:approve_once") is None
    assert parse_callback("appr:9") is None
    assert parse_callback("appr:notanint:approve_once") is None
    assert parse_callback("appr:9:bogus_action") is None


# ---- _on_callback ------------------------------------------------------------


async def test_on_callback_owner_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    update = _callback_update(user_id=OWNER_ID, data="appr:9:approve_once")

    await adapter._on_callback(update, _CTX)

    assert resolver.resolved == [(9, ApprovalAction.APPROVE_ONCE, str(OWNER_ID))]
    update.callback_query.answer.assert_awaited_once()  # type: ignore[union-attr]


async def test_on_callback_ignores_guest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    update = _callback_update(user_id=7, data="appr:9:approve_once")

    await adapter._on_callback(update, _CTX)

    assert resolver.resolved == []
    update.callback_query.answer.assert_awaited_once_with("Not allowed.")  # type: ignore[union-attr]


async def test_on_callback_ignores_foreign_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resolver = FakeResolver()
    adapter = _adapter(session_factory, FakeEngine(), approvals=resolver)
    update = _callback_update(user_id=OWNER_ID, data="other:9:approve_once")

    await adapter._on_callback(update, _CTX)

    assert resolver.resolved == []
    update.callback_query.answer.assert_awaited_once()  # type: ignore[union-attr]
