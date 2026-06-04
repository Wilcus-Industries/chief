"""Telegram long-poll adapter.

Normalizes inbound updates into :class:`Message`, classifies the sender's tier by id,
and records the contact. Owner messages are routed into the task engine (M2): a forum
topic *is* a task, the General topic is the casual inbox, and the engine decides whether
a casual message warrants its own tracked topic. Guests still get the minimal canned ack
(the full receptionist scope is M6).

Outbound text flows back through :class:`TelegramTaskIO`, the engine's
:class:`~chief.core.tasks.TaskIO` implementation: ``thread_key`` is
``"{chat_id}:{thread_id}"`` (``thread_id`` 0 = General / a flat DM), and create/archive
map to forum topics.
"""

import asyncio
import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..gate.approvals import ApprovalAction, ApprovalCard
from ..memory.store import OWNER_NAMESPACE, Fact
from ..persistence.contacts import get_or_create_contact
from ..persistence.models import Task
from .base import Adapter, Message, ReadyHook, Tier, classify_tier

logger = logging.getLogger("chief.adapters.telegram")

PLATFORM = "telegram"
TELEGRAM_LIMIT = 4096
TOPIC_NAME_LIMIT = 128
CALLBACK_PREFIX = "appr"  # approval button callback_data: "appr:{id}:{action}"


class Engine(Protocol):
    """The slice of :class:`~chief.core.tasks.TaskManager` the adapter drives."""

    async def dispatch(
        self, *, thread_key: str, text: str, is_general: bool = False
    ) -> None: ...
    async def cancel(self, thread_key: str) -> bool: ...
    async def active_tasks(self) -> list[Task]: ...


class ApprovalResolver(Protocol):
    """The slice of :class:`~chief.gate.approvals.ApprovalManager` button taps call."""

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> None: ...


class MemoryReader(Protocol):
    """The slice of :class:`~chief.memory.store.MemoryStore` the commands touch."""

    def list_facts(self, namespace: str) -> list[Fact]: ...
    async def forget(self, namespace: str, query: str) -> list[Fact]: ...


def _approval_keyboard(approval_id: int) -> InlineKeyboardMarkup:
    """The four-button approval card keyboard (DESIGN: self-curating gate)."""

    def button(label: str, action: ApprovalAction) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label, callback_data=f"{CALLBACK_PREFIX}:{approval_id}:{action.value}"
        )

    return InlineKeyboardMarkup(
        [
            [
                button("✅ Approve once", ApprovalAction.APPROVE_ONCE),
                button("❌ Deny once", ApprovalAction.DENY_ONCE),
            ],
            [
                button("⭐ Always allow", ApprovalAction.ALWAYS_ALLOW),
                button("🚫 Always deny", ApprovalAction.ALWAYS_DENY),
            ],
        ]
    )


def parse_callback(data: str) -> tuple[int, ApprovalAction] | None:
    """Decode approval ``callback_data``, or ``None`` if it is not ours / malformed."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), ApprovalAction(parts[2])
    except ValueError:
        return None


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split ``text`` into chunks within Telegram's per-message limit.

    Minimal hard split (M2); smart/file-aware splitting is M8.
    """
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _parse(thread_key: str) -> tuple[int, int]:
    chat_str, thread_str = thread_key.split(":")
    return int(chat_str), int(thread_str)


class TelegramTaskIO:
    """Engine → Telegram output: send text and manage forum topics."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, thread_key: str, text: str) -> None:
        chat_id, thread_id = _parse(thread_key)
        for chunk in split_message(text):
            await self._bot.send_message(
                chat_id=chat_id, text=chunk, message_thread_id=thread_id or None
            )

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        chat_id, _ = _parse(like_thread_key)
        topic = await self._bot.create_forum_topic(
            chat_id=chat_id, name=title[:TOPIC_NAME_LIMIT]
        )
        return f"{chat_id}:{topic.message_thread_id}"

    async def archive_thread(self, thread_key: str) -> None:
        chat_id, thread_id = _parse(thread_key)
        if thread_id == 0:
            return  # General / flat DM has no closable topic
        await self._bot.close_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id
        )

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        """Post an approval card to ``route`` (a ``thread_key``); return its msg ref."""
        chat_id, thread_id = _parse(route)
        sent = await self._bot.send_message(
            chat_id=chat_id,
            text=card.text,
            message_thread_id=thread_id or None,
            reply_markup=_approval_keyboard(card.approval_id),
        )
        return f"{chat_id}:{sent.message_id}"

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Rewrite a posted card to its outcome, dropping the now-spent buttons."""
        chat_str, message_str = msg_ref.split(":")
        await self._bot.edit_message_text(
            text=text, chat_id=int(chat_str), message_id=int(message_str)
        )


class TelegramAdapter(Adapter):
    """Owner-aware Telegram adapter that routes messages into the task engine."""

    def __init__(
        self,
        *,
        application: Application,  # type: ignore[type-arg]
        engine: Engine,
        owner_id: int,
        guest_ack: str,
        session_factory: async_sessionmaker[AsyncSession],
        approvals: ApprovalResolver | None = None,
        memory: MemoryReader | None = None,
    ) -> None:
        self._app = application
        self._engine = engine
        self._owner_id = owner_id
        self._guest_ack = guest_ack
        self._session_factory = session_factory
        self._approvals = approvals
        self._memory = memory
        self._stop = asyncio.Event()
        self._register()

    def _register(self) -> None:
        self._app.add_handler(CommandHandler("cancel", self._on_cancel))
        self._app.add_handler(CommandHandler("tasks", self._on_tasks))
        self._app.add_handler(CommandHandler("memory", self._on_memory))
        self._app.add_handler(CommandHandler("forget", self._on_forget))
        self._app.add_handler(
            CallbackQueryHandler(self._on_callback, pattern=f"^{CALLBACK_PREFIX}:")
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

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

    async def _record(self, message: Message) -> None:
        async with self._session_factory() as session:
            await get_or_create_contact(
                session,
                platform=message.platform,
                user_id=str(message.sender_id),
                tier=message.tier.value,
                display_name=message.sender_name,
            )

    async def _on_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = self.to_message(update)
        if message is None:
            return
        await self._record(message)
        if message.tier is not Tier.OWNER:
            logger.info("guest message", extra={"sender_id": message.sender_id})
            if update.effective_message is not None:
                await update.effective_message.reply_text(self._guest_ack)
            return
        chat = update.effective_chat
        is_general = bool(getattr(chat, "is_forum", False)) and (
            message.thread_key.endswith(":0")
        )
        logger.info("owner message", extra={"thread_key": message.thread_key})
        await self._engine.dispatch(
            thread_key=message.thread_key, text=message.text, is_general=is_general
        )

    def _owner_thread(self, update: Update) -> str | None:
        """Return the thread_key for an owner command, or ``None`` to ignore it."""
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return None
        if classify_tier(sender_id=user.id, owner_id=self._owner_id) is not Tier.OWNER:
            return None
        return f"{chat.id}:{message.message_thread_id or 0}"

    async def _on_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        thread_key = self._owner_thread(update)
        if thread_key is None or update.effective_message is None:
            return
        stopped = await self._engine.cancel(thread_key)
        await update.effective_message.reply_text(
            "Cancelled." if stopped else "Nothing running here."
        )

    async def _on_tasks(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._owner_thread(update) is None or update.effective_message is None:
            return
        tasks = await self._engine.active_tasks()
        if not tasks:
            await update.effective_message.reply_text("No active tasks.")
            return
        lines = [f"• {t.title or t.thread_key} — {t.status}" for t in tasks]
        await update.effective_message.reply_text("\n".join(lines))

    async def _on_memory(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List the owner's stored facts (owner only)."""
        message = update.effective_message
        if self._owner_thread(update) is None or message is None:
            return
        if self._memory is None:
            await message.reply_text("Memory isn't enabled.")
            return
        facts = self._memory.list_facts(OWNER_NAMESPACE)
        if not facts:
            await message.reply_text("No memories yet.")
            return
        await message.reply_text("\n".join(f"• {f.title}" for f in facts))

    async def _on_forget(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forget owner facts matching the command argument (owner only)."""
        message = update.effective_message
        if self._owner_thread(update) is None or message is None:
            return
        if self._memory is None:
            await message.reply_text("Memory isn't enabled.")
            return
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await message.reply_text("Usage: /forget <text>")
            return
        removed = await self._memory.forget(OWNER_NAMESPACE, query)
        if not removed:
            await message.reply_text("Nothing matched.")
            return
        await message.reply_text("Forgot: " + ", ".join(f.title for f in removed))

    async def _on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resolve an approval card button tap (owner only)."""
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None or query.data is None:
            return
        if classify_tier(sender_id=user.id, owner_id=self._owner_id) is not Tier.OWNER:
            await query.answer("Not allowed.")
            return
        parsed = parse_callback(query.data)
        if parsed is None or self._approvals is None:
            await query.answer()
            return
        approval_id, action = parsed
        await self._approvals.resolve(
            approval_id, action, decided_by=str(user.id)
        )
        await query.answer()

    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """Start polling and run until :meth:`stop` is called (shares the loop)."""
        app = self._app
        if app.updater is None:  # pragma: no cover - builder always sets it
            raise RuntimeError("application built without an updater")
        logger.info("telegram adapter starting (long-poll)")
        async with app:
            await app.start()
            await app.updater.start_polling()
            if on_ready is not None:
                await on_ready()
            await self._stop.wait()
            await app.updater.stop()
            await app.stop()

    async def stop(self) -> None:
        """Signal :meth:`run` to shut the connection down."""
        self._stop.set()
