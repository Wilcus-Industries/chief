"""Telegram adapter: routing into the engine, commands, and TaskIO output."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import Application

from chief.adapters.base import parse_callback
from chief.adapters.telegram import (
    TelegramAdapter,
    TelegramTaskIO,
    split_message,
)
from chief.gate.approvals import ApprovalAction, ApprovalCard
from chief.memory.store import Fact
from chief.persistence.models import Contact, Task

OWNER_ID = 42
_CTX = cast(Any, SimpleNamespace())


class FakeEngine:
    def __init__(
        self, *, active: list[Task] | None = None, cancel: bool = True
    ) -> None:
        self.dispatched: list[tuple[str, str, bool]] = []
        self.cancelled: list[str] = []
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


def _adapter(
    session_factory: async_sessionmaker[AsyncSession],
    engine: FakeEngine,
    *,
    approvals: FakeResolver | None = None,
    memory: FakeMemory | None = None,
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
    )


def _callback_update(*, user_id: int, data: str) -> Update:
    update = SimpleNamespace(
        callback_query=SimpleNamespace(data=data, answer=AsyncMock()),
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
) -> Update:
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            text=text, message_thread_id=thread_id, reply_text=AsyncMock()
        ),
        effective_user=SimpleNamespace(id=user_id, full_name="Someone"),
        effective_chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )
    return cast(Update, update)


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


async def test_guest_message_acks_without_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FakeEngine()
    adapter = _adapter(session_factory, engine)
    update = _fake_update(user_id=7, text="hello", thread_id=None)

    await adapter._on_message(update, _CTX)

    assert engine.dispatched == []
    update.effective_message.reply_text.assert_awaited_once_with("noted, thanks")  # type: ignore[union-attr]
    contacts = await _contacts(session_factory)
    assert contacts[0].tier == "guest"


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
