"""Telegram adapter routing and update normalization."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update

from chief.adapters.base import Message, Tier
from chief.adapters.telegram import TelegramAdapter
from chief.persistence.models import Contact

OWNER_ID = 42


def _adapter(
    session_factory: async_sessionmaker[AsyncSession], agent_fn: AsyncMock
) -> TelegramAdapter:
    return TelegramAdapter(
        token="x:y",
        owner_id=OWNER_ID,
        owner_model="claude-sonnet-4-6",
        guest_ack="noted, thanks",
        session_factory=session_factory,
        agent_fn=agent_fn,
    )


def _fake_update(
    *, user_id: int, text: str | None, thread_id: int | None = None
) -> Update:
    update = SimpleNamespace(
        effective_message=SimpleNamespace(text=text, message_thread_id=thread_id),
        effective_user=SimpleNamespace(id=user_id, full_name="Someone"),
        effective_chat=SimpleNamespace(id=-100),
    )
    return cast(Update, update)


async def _contacts(session_factory: async_sessionmaker[AsyncSession]) -> list[Contact]:
    async with session_factory() as session:
        return list((await session.execute(select(Contact))).scalars())


async def test_owner_message_calls_agent_and_records_contact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_fn = AsyncMock(return_value="agent reply")
    adapter = _adapter(session_factory, agent_fn)
    message = Message(
        platform="telegram",
        sender_id=OWNER_ID,
        text="hi",
        thread_key="-100:0",
        tier=Tier.OWNER,
    )

    reply = await adapter.handle(message)

    assert reply.text == "agent reply"
    agent_fn.assert_awaited_once_with("hi", model="claude-sonnet-4-6")
    contacts = await _contacts(session_factory)
    assert len(contacts) == 1
    assert contacts[0].tier == "owner"


async def test_guest_message_acks_without_calling_agent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_fn = AsyncMock(return_value="should not be used")
    adapter = _adapter(session_factory, agent_fn)
    message = Message(
        platform="telegram",
        sender_id=7,
        text="hello",
        thread_key="-100:0",
        tier=Tier.GUEST,
    )

    reply = await adapter.handle(message)

    assert reply.text == "noted, thanks"
    agent_fn.assert_not_awaited()
    contacts = await _contacts(session_factory)
    assert contacts[0].tier == "guest"


def test_to_message_classifies_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, AsyncMock())

    message = adapter.to_message(_fake_update(user_id=OWNER_ID, text="hi", thread_id=5))

    assert message is not None
    assert message.tier is Tier.OWNER
    assert message.thread_key == "-100:5"


def test_to_message_ignores_non_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = _adapter(session_factory, AsyncMock())

    assert adapter.to_message(_fake_update(user_id=OWNER_ID, text=None)) is None
