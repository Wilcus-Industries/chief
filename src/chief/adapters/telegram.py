"""Telegram long-poll adapter.

Normalizes inbound updates into :class:`Message`, classifies the sender's tier by id,
and records the contact. Owner messages are routed into the task engine (M2): a forum
topic *is* a task, the General topic is the casual inbox, and the engine decides whether
a casual message warrants its own tracked topic. Guests run through the M6 receptionist
gate (admission, rate limits, block/mute, dispatch) when ``guest_enabled``; otherwise
they get the canned ack.

Outbound text flows back through :class:`TelegramTaskIO`, the engine's
:class:`~chief.core.tasks.TaskIO` implementation: ``thread_key`` is
``"{chat_id}:{thread_id}"`` (``thread_id`` 0 = General / a flat DM), and create/archive
map to forum topics.
"""

import asyncio
import logging
from dataclasses import replace
from io import BytesIO
from typing import Any

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
from ..memory.store import OWNER_NAMESPACE
from ..persistence.contacts import get_or_create_contact
from .base import (
    ADMISSION_PREFIX,
    BUDGET_BUTTON_LABELS,
    BUDGET_PREFIX,
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
    Tier,
    admission_payload,
    apply_admission,
    apply_budget_decision,
    budget_outcome_text,
    budget_payload,
    classify_tier,
    default_branch_title,
    handle_guest_message,
    is_supported_media,
    parse_admission,
    parse_budget,
    parse_callback,
)
from .base import (
    split_message as _split,
)

logger = logging.getLogger("chief.adapters.telegram")

PLATFORM = "telegram"
TELEGRAM_LIMIT = 4096
TOPIC_NAME_LIMIT = 128

#: Button payloads ``_on_callback`` claims: approval (``appr:``), admission (``adm:``),
#: AND budget choice (``bud:``) cards. python-telegram-bot only dispatches callbacks
#: matching this regex, so it must cover every prefix or a card's taps are silently
#: dropped.
CALLBACK_QUERY_PATTERN = (
    rf"^(?:{CALLBACK_PREFIX}|{ADMISSION_PREFIX}|{BUDGET_PREFIX}):"
)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram-defaulted wrapper over the shared base ``split_message``."""
    return _split(text, limit)


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


def _admission_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    """The two-button first-contact card (M6): admit the guest, or block them."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Admit",
                    callback_data=admission_payload(
                        contact_id, AdmissionAction.ADMIT
                    ),
                ),
                InlineKeyboardButton(
                    "🚫 Block",
                    callback_data=admission_payload(
                        contact_id, AdmissionAction.BLOCK
                    ),
                ),
            ]
        ]
    )


def _budget_keyboard(cycle: str) -> InlineKeyboardMarkup:
    """The three-button budget choice card (M9): downgrade / continue / overflow."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BUDGET_BUTTON_LABELS[action],
                    callback_data=budget_payload(cycle, action),
                )
                for action in BudgetAction
            ]
        ]
    )


def _parse(thread_key: str) -> tuple[int, int]:
    chat_str, thread_str = thread_key.split(":")
    return int(chat_str), int(thread_str)


def _has_media(message: Any) -> bool:
    """True if the Telegram message carries a photo or a document (M8 intake)."""
    return bool(message.photo) or message.document is not None


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

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Upload ``data`` as a document to the thread (long-reply file output, M8)."""
        chat_id, thread_id = _parse(thread_key)
        document = BytesIO(data)
        document.name = filename
        await self._bot.send_document(
            chat_id=chat_id,
            document=document,
            filename=filename,
            caption=caption,
            message_thread_id=thread_id or None,
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

    async def send_admission_card(self, route: str, card: AdmissionCard) -> None:
        """Post a first-contact admit/block card to ``route`` (M6, no ref tracked)."""
        chat_id, thread_id = _parse(route)
        await self._bot.send_message(
            chat_id=chat_id,
            text=card.text,
            message_thread_id=thread_id or None,
            reply_markup=_admission_keyboard(card.contact_id),
        )

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Post the budget choice card to ``route`` (M9, no ref tracked)."""
        chat_id, thread_id = _parse(route)
        await self._bot.send_message(
            chat_id=chat_id,
            text=card.text,
            message_thread_id=thread_id or None,
            reply_markup=_budget_keyboard(card.cycle),
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
        io: TelegramTaskIO | None = None,
        guest_enabled: bool = False,
        front_desk_thread_key: str | None = None,
        guest_rate: int = 10,
        guest_rate_window: int = 3600,
        guest_global_rate: int = 60,
    ) -> None:
        self._app = application
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
        # Contacts already prompted this run — dedupe the admission card so a pending
        # guest who keeps messaging doesn't re-card the owner (their notes still relay).
        self._prompted_admission: set[int] = set()
        self._stop = asyncio.Event()
        self._register()

    def _register(self) -> None:
        self._app.add_handler(CommandHandler("cancel", self._on_cancel))
        self._app.add_handler(CommandHandler("tasks", self._on_tasks))
        self._app.add_handler(CommandHandler("memory", self._on_memory))
        self._app.add_handler(CommandHandler("forget", self._on_forget))
        self._app.add_handler(CommandHandler("branch", self._on_branch))
        self._app.add_handler(
            CallbackQueryHandler(self._on_callback, pattern=CALLBACK_QUERY_PATTERN)
        )
        # Photos/documents join text so owner image+PDF intake (M8) reaches _on_message;
        # a bare photo carries no TEXT, so the old TEXT-only filter would drop it.
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL)
                & ~filters.COMMAND,
                self._on_message,
            )
        )

    def to_message(self, update: Update) -> Message | None:
        """Normalize an update into a :class:`Message`, or ``None`` to ignore it.

        Attachments are *not* downloaded here (this is sync) — :meth:`_on_message`
        pulls owner media before dispatch. A media-only message (no text) is kept; its
        caption, if any, becomes the text.
        """
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return None
        text = message.text or message.caption or ""
        if not text and not _has_media(message):
            return None
        thread_id = message.message_thread_id or 0
        return Message(
            platform=PLATFORM,
            sender_id=user.id,
            text=text,
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
            tg_message = update.effective_message
            if not self._guest_enabled or self._io is None:
                if tg_message is not None:
                    await tg_message.reply_text(self._guest_ack)
                return

            async def reply(text: str) -> None:
                if tg_message is not None:
                    await tg_message.reply_text(text)

            await self._handle_guest(message, reply)
            return
        tg_message = update.effective_message
        if tg_message is not None:
            attachments = await self._owner_attachments(tg_message)
            if attachments:
                message = replace(message, attachments=attachments)
        chat = update.effective_chat
        is_general = bool(getattr(chat, "is_forum", False)) and (
            message.thread_key.endswith(":0")
        )
        logger.info("owner message", extra={"thread_key": message.thread_key})
        await self._engine.dispatch(
            thread_key=message.thread_key,
            text=message.text,
            attachments=message.attachments,
            is_general=is_general,
        )

    async def _owner_attachments(self, tg_message: Any) -> tuple[Attachment, ...]:
        """Download the owner's image/PDF files (≤ caps); skip anything else (M8).

        Photos arrive as ascending ``PhotoSize``s — the last is the highest-res, the one
        worth sending. Documents keep their declared mime/filename. Over-cap files are
        dropped silently rather than failing the whole turn.
        """
        items: list[Attachment] = []
        if tg_message.photo:
            att = await self._download_tg(tg_message.photo[-1], "image/jpeg", None)
            if att is not None:
                items.append(att)
        doc = tg_message.document
        if doc is not None and doc.mime_type and is_supported_media(doc.mime_type):
            att = await self._download_tg(doc, doc.mime_type, doc.file_name)
            if att is not None:
                items.append(att)
        return tuple(items[:MAX_ATTACHMENTS])

    @staticmethod
    async def _download_tg(
        source: Any, media_type: str, filename: str | None
    ) -> Attachment | None:
        """Fetch a Telegram file's bytes, or ``None`` if it is over the size cap."""
        if (getattr(source, "file_size", None) or 0) > MAX_ATTACHMENT_BYTES:
            return None
        handle = await source.get_file()
        data = bytes(await handle.download_as_bytearray())
        if len(data) > MAX_ATTACHMENT_BYTES:
            return None
        return Attachment(media_type=media_type, data=data, filename=filename)

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

    async def _on_branch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Promote the casual channel into a tracked thread (owner + casual only).

        Only the General/``:0`` channel carries the lossy self-compacting context worth
        promoting; a real topic is already tracked, so ``/branch`` there is a no-op
        reply. The optional argument names the new thread; otherwise a timestamp does.
        """
        thread_key = self._owner_thread(update)
        message = update.effective_message
        chat = update.effective_chat
        if thread_key is None or message is None or chat is None:
            return
        # The casual lane is the forum's General topic — the same is_forum AND :0
        # predicate _on_message uses for is_general. A flat DM also keys to :0 but runs
        # as a normal archiving task, so /branch must not treat it as casual.
        is_casual = bool(getattr(chat, "is_forum", False)) and thread_key.endswith(":0")
        if not is_casual:
            await message.reply_text("/branch only works in the casual channel.")
            return
        title = (message.text or "").partition(" ")[2].strip() or default_branch_title()
        await self._engine.branch(thread_key, title)
        await message.reply_text(f'→ Branched into "{title}".')

    async def _on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resolve an approval, admission, or budget card button tap (owner only)."""
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None or query.data is None:
            return
        if classify_tier(sender_id=user.id, owner_id=self._owner_id) is not Tier.OWNER:
            await query.answer("Not allowed.")
            return
        parsed = parse_callback(query.data)
        if parsed is not None and self._approvals is not None:
            approval_id, action = parsed
            await self._approvals.resolve(approval_id, action, decided_by=str(user.id))
            await query.answer()
            return
        admission = parse_admission(query.data)
        if admission is not None:
            await self._resolve_admission(query, *admission)
            return
        budget = parse_budget(query.data)
        if budget is not None:
            await self._resolve_budget(query, *budget)
            return
        await query.answer()

    async def _resolve_admission(
        self, query: Any, contact_id: int, action: AdmissionAction
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
        try:
            await query.edit_message_text(outcome)
        except Exception:  # editing is best-effort; the decision already persisted
            logger.debug("admission card edit failed", exc_info=True)
        await query.answer()

    async def _resolve_budget(
        self, query: Any, cycle: str, action: BudgetAction
    ) -> None:
        """Apply a budget choice and rewrite the card to its outcome (owner only)."""
        await apply_budget_decision(
            self._session_factory, cycle=cycle, action=action
        )
        try:
            await query.edit_message_text(budget_outcome_text(action))
        except Exception:  # editing is best-effort; the mode already persisted
            logger.debug("budget card edit failed", exc_info=True)
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
