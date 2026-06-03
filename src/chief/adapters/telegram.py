"""Telegram long-poll adapter.

Normalizes inbound updates into :class:`Message`, classifies the sender's tier by id,
records the contact, then routes: the owner gets a real one-shot agent reply; a guest
gets a minimal canned acknowledgement (the full receptionist scope is M6). Topics,
steering, and persistent sessions arrive at M2 — here ``thread_key`` is recorded but
every owner message is a fresh one-shot turn.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..core.agent import owner_oneshot
from ..persistence.contacts import get_or_create_contact
from .base import Adapter, Message, Reply, Tier, classify_tier

logger = logging.getLogger("chief.adapters.telegram")

PLATFORM = "telegram"

# Run one owner turn: (prompt, model) -> reply text. Injected so tests can stub the SDK.
AgentFn = Callable[..., Awaitable[str]]


class TelegramAdapter(Adapter):
    """Owner-aware Telegram adapter over python-telegram-bot long-polling."""

    def __init__(
        self,
        *,
        token: str,
        owner_id: int,
        owner_model: str,
        guest_ack: str,
        session_factory: async_sessionmaker[AsyncSession],
        agent_fn: AgentFn = owner_oneshot,
    ) -> None:
        self._token = token
        self._owner_id = owner_id
        self._owner_model = owner_model
        self._guest_ack = guest_ack
        self._session_factory = session_factory
        self._agent_fn = agent_fn

    def to_message(self, update: Update) -> Message | None:
        """Normalize an update into a :class:`Message`, or ``None`` to ignore it."""
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or not message.text:
            return None
        thread_id = message.message_thread_id or 0
        return Message(
            platform=PLATFORM,
            sender_id=user.id,
            text=message.text,
            thread_key=f"{chat.id}:{thread_id}",
            tier=classify_tier(sender_id=user.id, owner_id=self._owner_id),
            sender_name=user.full_name,
        )

    async def handle(self, message: Message) -> Reply:
        """Record the contact and produce the tier-appropriate reply."""
        async with self._session_factory() as session:
            await get_or_create_contact(
                session,
                platform=message.platform,
                user_id=str(message.sender_id),
                tier=message.tier.value,
                display_name=message.sender_name,
            )

        if message.tier is Tier.OWNER:
            logger.info("owner message", extra={"thread_key": message.thread_key})
            text = await self._agent_fn(message.text, model=self._owner_model)
            return Reply(text=text)

        logger.info("guest message", extra={"sender_id": message.sender_id})
        return Reply(text=self._guest_ack)

    async def _on_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = self.to_message(update)
        if message is None:
            return
        reply = await self.handle(message)
        if update.effective_message is not None:
            await update.effective_message.reply_text(reply.text)

    def run(self) -> None:
        """Build the application and long-poll until stopped (owns the event loop)."""
        app = Application.builder().token(self._token).build()
        handler = MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        app.add_handler(handler)
        logger.info("telegram adapter starting (long-poll)")
        app.run_polling()
