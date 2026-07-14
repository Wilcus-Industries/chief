"""iMessage adapter (#156): text chief like a real assistant, macOS-only.

Built on a **dedicated Apple ID** — the Messages account signed in on chief's Mac
*is* chief's identity; it texts as itself and never ghost-writes as the owner.

- **Inbound** is a poll loop inside :meth:`IMessageAdapter.run` (the scheduler's
  tick-loop shape) watching the local Messages store through the #155 read seam
  (:meth:`~chief.tools.apple.runner.ScriptRunner.run_sqlite`, read-only sqlite3)
  with a persisted cursor (:mod:`chief.persistence.imessage`), so restarts neither
  replay old texts nor drop ones that arrived while down. The **whitelist is the
  event gate**: only handles with an ``imessage`` Contact row raise events — owner
  handles run the full owner path (including ``/commands``), guest handles run the
  shared M6 guest gate, and anyone else gets no session, no reply, and a
  metadata-only ``unknown_senders`` line (handle + timestamp, never content). DMs
  only: group-chat rows are skipped wholesale, never read into anything.
- **Outbound** goes through Messages via OS automation on the same runner seam
  (:data:`SEND_TEXT_SCRIPT` — a fixed JXA constant, owner data strictly as argv).
  A DM thread's ``thread_key`` is the normalized handle, and the stack's engine IO
  is mirror-wrapped in :mod:`chief.app`, so iMessage conversations appear live in
  the terminal client and web UI like every other surface.
- **Delegation modes** per guest conversation: ``auto`` (default) or draft-first —
  :class:`DraftFirstIO` sits *outside* the mirror and parks each guest-directed
  outbound on a :data:`~chief.gate.approvals.DRAFT_SEND_KIND` approval card
  (approve sends + mirrors, deny kills it clean). The first send to a
  never-contacted guest handle raises the card regardless of mode.

iMessage renders no buttons, so cards sent here are text renderings; the mirror's
socket broadcast is what makes them answerable (web UI / terminal, #136).

**Self-DM mode** (#161, opt-in via ``imessage_self_dm``) relaxes the dedicated-ID
assumption for a personal Apple ID: the owner texts chief inside the *self-chat*
(a message to their own number), whose received copy already routes owner-tier. To
keep chief distinct from the owner in that shared thread — and to break the reply
loop it creates (chief's own reply re-enters the store as another inbound row) —
every reply to a self-handle is prefixed :data:`BOT_PREFIX` and passed through a
two-layer echo filter: a durable send-record (``imessage_sends``, matched and
consumed when the echo re-polls) plus a stateless prefix skip. With the flag off,
none of this engages and the dedicated-ID behavior above holds byte-for-byte.
Self-DM media intake (#162) rides this same self-thread gate: an image/PDF sent to
self is admitted (even textless) and threaded into the turn exactly like the other
adapters' owner attachments — dedicated-ID mode stays text-only, byte-for-byte.
"""

import asyncio
import json
import logging
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..gate.approvals import DRAFT_SEND_KIND, ApprovalCard
from ..persistence import imessage as repo
from ..persistence.contacts import get_contact
from ..tools.apple.doctor import CapabilityHealth
from ..tools.apple.runner import ScriptRunner
from .base import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    Adapter,
    AdmissionCard,
    Attachment,
    BudgetCard,
    Engine,
    MemoryReader,
    Message,
    ReadyHook,
    Surface,
    Tier,
    is_supported_media,
)
from .base import (
    handle_guest_message as _handle_guest_message,
)
from .base import (
    split_message as _split,
)
from .commands import OWNER_COMMANDS, CommandContext, CommandRegistry
from .mirror import PlatformIO

logger = logging.getLogger("chief.adapters.imessage")

PLATFORM = repo.PLATFORM

#: The split-vs-file cap. Messages accepts far longer texts, but multi-thousand-char
#: bubbles read terribly on a phone; longer replies split (and very long ones ship
#: as a file note — see ``send_file``).
IMESSAGE_LIMIT = 4000

#: Prefix on every self-DM reply (#161): the owner-visible "this is chief, not you"
#: marker inside the shared self-chat, and the stateless half of the echo filter —
#: an inbound row bearing it is chief's own echo and is never dispatched.
BOT_PREFIX = "🤖 "

#: HEIC/HEIF need conversion before a vision-model turn will accept them (#162);
#: everything else `is_supported_media` allows passes through unchanged.
HEIC_MIME_TYPES = frozenset({"image/heic", "image/heif"})

#: Rows pulled per poll tick — bounds one tick's work; the cursor picks up the rest.
POLL_BATCH_LIMIT = 200

#: Consecutive poll failures before the owner is alerted at the Front Desk (the
#: poller must fail loud, not silently stop delivering texts).
ALERT_AFTER_FAILURES = 3

#: The doctor capabilities the adapter needs green before it boots (#155 pattern):
#: Full Disk Access for the store read, Automation → Messages for the send path.
REQUIRED_CAPABILITIES = ("messages", "messages_send")

#: Fixed JXA send scripts (never spliced — the handle and text/file path travel as
#: argv, so there is no quoting or injection surface; see tools/apple/runner.py).
#: An existing conversation resolves via ``participants``; a first-ever outbound
#: falls back to the iMessage account's participant-by-id form. The env-gated live
#: suite (tests/test_imessage_live.py) is the canary for Apple changing this
#: scripting dictionary.
SEND_TEXT_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Messages');\n"
    "  const matches = app.participants.whose({handle: argv[0]})();\n"
    "  let target = matches.length > 0 ? matches[0] : null;\n"
    "  if (target === null) {\n"
    "    const account = app.accounts.whose({serviceType: 'iMessage'})()[0];\n"
    "    target = account.participants.byId('iMessage;-;' + argv[0]);\n"
    "  }\n"
    "  app.send(argv[1], {to: target});\n"
    "  return 'sent';\n"
    "}"
)
SEND_FILE_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Messages');\n"
    "  const matches = app.participants.whose({handle: argv[0]})();\n"
    "  let target = matches.length > 0 ? matches[0] : null;\n"
    "  if (target === null) {\n"
    "    const account = app.accounts.whose({serviceType: 'iMessage'})()[0];\n"
    "    target = account.participants.byId('iMessage;-;' + argv[0]);\n"
    "  }\n"
    "  app.send(Path(argv[1]), {to: target});\n"
    "  return 'sent';\n"
    "}"
)


def build_poll_query(after_rowid: int, limit: int = POLL_BATCH_LIMIT) -> str:
    """The incremental inbound query: new, real, other-people texts (or attachments)
    past the cursor.

    Both parameters are integers coerced with ``int()`` — no owner or sender text
    ever reaches this SQL, so there is no escaping surface (the same posture as the
    #155 read tools, which quote+LIKE-escape their one text parameter).
    ``is_from_me = 0`` keeps chief's own sends out; ``associated_message_type = 0``
    drops tapbacks/edits; the chat join classifies DM-vs-group (``style`` 45 with no
    ``room_name`` is a 1:1) so group rows can be skipped wholesale. A row admits with
    either non-empty text or a real (non-rich-link) attachment join — sender-agnostic;
    the self-DM-only restriction on textless admission is applied in Python, not here
    (#162). Timestamps render as UTC.
    """
    return (
        "SELECT message.ROWID AS rowid, handle.id AS sender, "
        "message.text AS text, "
        "datetime(message.date/1000000000 + strftime('%s','2001-01-01'), "
        "'unixepoch') AS timestamp, "
        "MAX(CASE WHEN chat.style IS NOT NULL AND chat.style != 45 "
        "THEN 1 ELSE 0 END) AS in_group, "
        "MAX(CASE WHEN chat.room_name IS NOT NULL THEN 1 ELSE 0 END) AS has_room "
        "FROM message "
        "JOIN handle ON message.handle_id = handle.ROWID "
        "LEFT JOIN chat_message_join "
        "ON chat_message_join.message_id = message.ROWID "
        "LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id "
        f"WHERE message.ROWID > {int(after_rowid)} "
        "AND message.is_from_me = 0 "
        "AND message.associated_message_type = 0 "
        "AND (message.text IS NOT NULL AND message.text != '' "
        "OR EXISTS (SELECT 1 FROM message_attachment_join maj "
        "JOIN attachment att ON att.ROWID = maj.attachment_id "
        "WHERE maj.message_id = message.ROWID AND att.mime_type IS NOT NULL)) "
        "GROUP BY message.ROWID "
        f"ORDER BY message.ROWID ASC LIMIT {int(limit)};"
    )


def build_head_query() -> str:
    """The store's current head ROWID — the first-boot cursor (no history replay)."""
    return "SELECT COALESCE(MAX(ROWID), 0) AS head FROM message;"


def build_attachments_query(rowids: Sequence[int]) -> str:
    """Real, non-rich-link attachments for a fetched batch of message rowids.

    ``rowids`` are int()-coerced (they come from the prior query's own ROWIDs, never
    owner/sender text), so there is no escaping surface here either.
    ``.pluginPayloadAttachment`` rich-link rows have ``mime_type`` NULL and are
    excluded — they are not user media (#162).
    """
    ids = ",".join(str(int(rowid)) for rowid in rowids)
    return (
        "SELECT message_attachment_join.message_id AS message_id, "
        "attachment.filename AS filename, attachment.mime_type AS mime_type, "
        "attachment.transfer_name AS transfer_name, "
        "attachment.total_bytes AS total_bytes "
        "FROM message_attachment_join "
        "JOIN attachment ON attachment.ROWID = message_attachment_join.attachment_id "
        f"WHERE message_attachment_join.message_id IN ({ids}) "
        "AND attachment.mime_type IS NOT NULL "
        "ORDER BY message_attachment_join.message_id, attachment.ROWID;"
    )


def imessage_ready(
    health: Sequence[CapabilityHealth],
) -> tuple[bool, str]:
    """Whether the #155 doctor probes clear the adapter to boot, and why not.

    Requires the Messages store read (Full Disk Access) and the Messages send
    automation grant both green — the two real boundaries the adapter drives.
    """
    by_capability = {item.capability: item for item in health}
    missing = [
        name
        for name in REQUIRED_CAPABILITIES
        if not (name in by_capability and by_capability[name].ok)
    ]
    if not missing:
        return True, ""
    details = "; ".join(
        f"{name}: {by_capability[name].status} ({by_capability[name].fix})"
        if name in by_capability
        else f"{name}: not probed"
        for name in missing
    )
    return False, details


def _card_text(prefix: str, body: str) -> str:
    """A button-less card rendering: iMessage shows text; answers come from the
    web UI / terminal client via the mirror's socket broadcast (#136)."""
    return f"{prefix}\n\n{body}\n\n(Answer from the chief web UI or terminal.)"


class IMessageTaskIO:
    """Engine → Messages output over the ScriptRunner seam (argv-only data).

    ``thread_key`` is the normalized handle. Cards render as text (no buttons on
    iMessage); the mirror wrapper is what makes them answerable elsewhere.
    """

    def __init__(
        self,
        runner: ScriptRunner,
        *,
        outbox_dir: str = "data/imessage_outbox",
        self_dm: bool = False,
        self_handles: frozenset[str] = frozenset(),
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._runner = runner
        #: Where outbound file attachments are written so Messages can pick them
        #: up (the JXA send takes a path; Messages reads it asynchronously, so the
        #: file must outlive the call).
        self._outbox_dir = Path(outbox_dir)
        #: Self-DM echo-filter state (#161): when on, every send to a self-handle is
        #: prefixed and recorded so its store echo can be consumed at re-poll.
        self._self_dm = self_dm
        self._self_handles = self_handles
        self._session_factory = session_factory

    def _is_self(self, handle: str) -> bool:
        return self._self_dm and repo.normalize_handle(handle) in self._self_handles

    async def _record(self, handle: str, body: str) -> None:
        """Durably record an own send to a self-handle (loop-proof echo filter)."""
        if self._is_self(handle) and self._session_factory is not None:
            async with self._session_factory() as session:
                await repo.record_send(session, handle, body)

    async def _send_raw(self, handle: str, text: str) -> None:
        result = await self._runner.run_jxa(SEND_TEXT_SCRIPT, [handle, text])
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"iMessage send to {handle} failed: {detail}")

    async def send(self, thread_key: str, text: str) -> None:
        for chunk in _split(text, IMESSAGE_LIMIT):
            # Every chunk (not just the first) is prefixed so each echoed chunk is
            # caught by the stateless filter (#161); off, this is a no-op.
            body = BOT_PREFIX + chunk if self._is_self(thread_key) else chunk
            await self._send_raw(thread_key, body)
            await self._record(thread_key, body)

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Write the bytes to the outbox and send them as a Messages attachment."""
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        path = self._outbox_dir / filename
        path.write_bytes(data)
        result = await self._runner.run_jxa(
            SEND_FILE_SCRIPT, [thread_key, str(path.resolve())]
        )
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"iMessage file send to {thread_key} failed: {detail}"
            )
        # Record the file send at send time (its echo carries the filename); the
        # caption then routes through send() and is prefixed + recorded there.
        await self._record(thread_key, filename)
        if caption:
            await self.send(thread_key, caption)

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        """iMessage DMs are flat — there is no topic to create; stay in-thread."""
        return like_thread_key

    async def archive_thread(self, thread_key: str) -> None:
        """No closable topic on a flat DM (mirrors the Telegram ``:0`` behavior)."""

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        """Post an approval card as text; the msg_ref is the route (no edit API)."""
        await self.send(route, _card_text("🔔 Approval needed:", card.text))
        return route

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Messages can't edit a sent bubble — post the outcome as a follow-up."""
        await self.send(msg_ref, text)

    async def send_admission_card(self, route: str, card: AdmissionCard) -> None:
        """First-contact prompt, as text (decide via web settings / manage tool)."""
        await self.send(route, _card_text("🔔 New contact:", card.text))

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        await self.send(route, _card_text("💳 Budget decision needed:", card.text))


class DraftApprover(Protocol):
    """The slice of :class:`~chief.gate.approvals.ApprovalManager` the draft gate
    parks on. ``request`` blocks until the owner decides (timeout ⇒ deny)."""

    async def request(
        self,
        *,
        task_id: int | None,
        thread_key: str,
        tier: str,
        tool_name: str,
        tool_input: dict[str, Any],
        route: str,
    ) -> bool: ...


class DraftFirstIO:
    """The delegation-mode gate over the engine's outbound (#156).

    Wraps the *mirrored* platform IO, so a killed draft is never delivered, never
    broadcast, and never logged — it simply doesn't happen. A guest-directed
    ``send``/``send_file`` parks on a :data:`DRAFT_SEND_KIND` approval card when
    the conversation is in draft mode **or** the handle has never been texted
    before (the first-send guard, regardless of mode). Owner-directed and
    non-whitelisted routes (the Front Desk, card outcomes) pass straight through.
    """

    def __init__(
        self,
        inner: PlatformIO,
        *,
        approvals: DraftApprover,
        session_factory: async_sessionmaker[AsyncSession],
        front_desk: str,
    ) -> None:
        self._inner = inner
        self._approvals = approvals
        self._session_factory = session_factory
        self._front_desk = front_desk

    async def _needs_card(self, thread_key: str) -> bool:
        async with self._session_factory() as session:
            contact = await get_contact(
                session, platform=PLATFORM, user_id=thread_key
            )
            if contact is None or contact.tier != repo.TIER_GUEST:
                return False  # only guest-directed sends are outward-facing here
            pref = await repo.get_pref(session, thread_key)
        if pref is None:
            return True  # never contacted and no mode row — fail to the card
        return pref.mode == repo.MODE_DRAFT or not pref.contacted

    async def _approve(self, thread_key: str, preview: str) -> bool:
        allowed = await self._approvals.request(
            task_id=None,
            thread_key=thread_key,
            tier=repo.TIER_GUEST,
            tool_name=DRAFT_SEND_KIND,
            tool_input={"to": thread_key, "text": preview},
            route=self._front_desk,
        )
        if allowed:
            async with self._session_factory() as session:
                await repo.mark_contacted(session, thread_key)
        else:
            logger.info(
                "draft killed", extra={"thread_key": thread_key}
            )
        return allowed

    async def send(self, thread_key: str, text: str) -> None:
        if await self._needs_card(thread_key) and not await self._approve(
            thread_key, text
        ):
            return
        await self._inner.send(thread_key, text)

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        preview = f"[file: {filename}]" + (f" {caption}" if caption else "")
        if await self._needs_card(thread_key) and not await self._approve(
            thread_key, preview
        ):
            return
        await self._inner.send_file(thread_key, filename, data, caption)

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        return await self._inner.create_thread(
            like_thread_key=like_thread_key, title=title
        )

    async def archive_thread(self, thread_key: str) -> None:
        await self._inner.archive_thread(thread_key)

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        return await self._inner.send_card(route, card)

    async def edit_card(self, msg_ref: str, text: str) -> None:
        await self._inner.edit_card(msg_ref, text)

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        await self._inner.send_budget_card(route, card)


class IMessageAdapter(Adapter):
    """Poll the Messages store and route whitelisted texts into the engine."""

    def __init__(
        self,
        *,
        runner: ScriptRunner,
        db_path: str,
        engine: Engine,
        session_factory: async_sessionmaker[AsyncSession],
        io: IMessageTaskIO,
        owner_handles: Sequence[str],
        guest_ack: str,
        guest_enabled: bool = False,
        guest_rate: int = 10,
        guest_rate_window: int = 3600,
        guest_global_rate: int = 60,
        poll_seconds: float = 2.0,
        memory: MemoryReader | None = None,
        commands: CommandRegistry | None = None,
        self_dm: bool = False,
    ) -> None:
        self._runner = runner
        self._db_path = db_path
        self._engine = engine
        self._session_factory = session_factory
        self._io = io
        self._owner_handles = tuple(
            repo.normalize_handle(handle) for handle in owner_handles
        )
        #: Self-DM inbound echo filter (#161): the owner's own handles are the
        #: self-chat, so an inbound from one that matches a recorded send (or
        #: carries the bot prefix) is chief's own echo — skipped, not dispatched.
        self._self_dm = self_dm
        self._self_handles = frozenset(self._owner_handles)
        #: Guest cards/relays route to the owner's own thread — the iMessage Front
        #: Desk is the first owner handle (the global ``front_desk_thread_key`` is
        #: another platform's key, unusable as a Messages target).
        self._front_desk = self._owner_handles[0] if self._owner_handles else None
        self._guest_ack = guest_ack
        self._guest_enabled = guest_enabled
        self._guest_rate = guest_rate
        self._guest_rate_window = guest_rate_window
        self._guest_global_rate = guest_global_rate
        self._poll_seconds = poll_seconds
        self._memory = memory
        self._commands = commands or OWNER_COMMANDS
        self._prompted_admission: set[int] = set()
        self._stop = asyncio.Event()
        self._failures = 0
        self._last_error: str | None = None

    # ---- lifecycle ---------------------------------------------------------------

    async def prime(self) -> None:
        """Seed the whitelist + initialize the cursor (idempotent, run() calls it).

        Owner handles from config land owner-tier (the installer wizard calls
        :func:`~chief.persistence.imessage.seed_owner_handles` with the same
        effect). A missing cursor initializes to the store's current head, so the
        first boot never replays the machine's existing history.
        """
        await repo.seed_owner_handles(self._session_factory, self._owner_handles)
        async with self._session_factory() as session:
            cursor = await repo.get_cursor(session, PLATFORM)
        if cursor is None:
            head = await self._store_head()
            async with self._session_factory() as session:
                await repo.set_cursor(session, PLATFORM, head)

    async def run(self, on_ready: ReadyHook | None = None) -> None:
        """The poll loop (the scheduler's tick shape, at the adapter's own cadence).

        A failed tick is logged loudly and — after :data:`ALERT_AFTER_FAILURES`
        consecutive failures — alerted to the owner's Front Desk once per streak,
        rather than silently stopping delivery; the loop itself keeps ticking so a
        transient store error (backup, migration) self-heals.
        """
        await self.prime()
        if on_ready is not None:
            await on_ready()
        logger.info("imessage adapter starting (store poll)")
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                logger.exception("imessage poll failed")
                await self._alert_if_failing()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_seconds
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        """Signal :meth:`run` to exit at the next tick boundary."""
        self._stop.set()

    def poll_status(self) -> tuple[bool, str]:
        """The poller's live health: ``(ok, detail)`` for the web health page."""
        if self._failures == 0:
            return True, "polling"
        return False, (
            f"{self._failures} consecutive poll failure(s): "
            f"{self._last_error or 'unknown'}"
        )

    async def _alert_if_failing(self) -> None:
        """Tell the owner once per failure streak — red, not silent."""
        if self._failures != ALERT_AFTER_FAILURES or self._front_desk is None:
            return
        try:
            await self._io.send(
                self._front_desk,
                "⚠️ The iMessage poller is failing and texts are not being "
                f"delivered: {self._last_error or 'unknown error'}. Run "
                "check_apple_health for the permission walk-through.",
            )
        except Exception:  # the send path may be the broken part — stay alive
            logger.exception("imessage failure alert could not be sent")

    # ---- polling -----------------------------------------------------------------

    async def _store_head(self) -> int:
        result = await self._runner.run_sqlite(self._db_path, build_head_query())
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"cannot read the Messages store head: {detail}")
        rows = json.loads(result.stdout) if result.stdout.strip() else []
        return int(rows[0]["head"]) if rows else 0

    async def poll_once(self) -> int:
        """One incremental tick: fetch past the cursor, route, advance, persist.

        The cursor advances past every *fetched* row, including ones whose
        handling raised (logged loudly and skipped) — a poisoned row must not
        wedge delivery for everyone behind it.
        """
        async with self._session_factory() as session:
            after = await repo.get_cursor(session, PLATFORM) or 0
        result = await self._runner.run_sqlite(
            self._db_path, build_poll_query(after)
        )
        if not result.ok:
            self._failures += 1
            self._last_error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"imessage poll query failed: {self._last_error}")
        rows = json.loads(result.stdout) if result.stdout.strip() else []
        attachments_by_rowid: dict[int, list[dict[str, Any]]] = {}
        if rows:
            att_result = await self._runner.run_sqlite(
                self._db_path,
                build_attachments_query([int(r["rowid"]) for r in rows]),
            )
            if att_result.ok and att_result.stdout.strip():
                for att_row in json.loads(att_result.stdout):
                    attachments_by_rowid.setdefault(
                        int(att_row["message_id"]), []
                    ).append(att_row)
            elif not att_result.ok:
                logger.warning(
                    "imessage attachment fetch failed: %s", att_result.stderr
                )
        for row in rows:
            try:
                await self._handle_row(
                    row, attachments_by_rowid.get(int(row["rowid"]), [])
                )
            except Exception:
                logger.exception(
                    "imessage row handling failed", extra={"rowid": row.get("rowid")}
                )
        if rows:
            async with self._session_factory() as session:
                await repo.set_cursor(
                    session, PLATFORM, int(rows[-1]["rowid"])
                )
        self._failures = 0
        self._last_error = None
        return len(rows)

    async def _handle_row(
        self, row: dict[str, Any], atts_raw: list[dict[str, Any]]
    ) -> None:
        if row.get("in_group") or row.get("has_room"):
            return  # DMs only in v1: group threads are never read, logged, or answered
        sender = repo.normalize_handle(str(row.get("sender") or ""))
        text = str(row.get("text") or "")
        if not sender or (not text and not atts_raw):
            return
        is_self_row = self._self_dm and sender in self._self_handles
        if not text and not is_self_row:
            return  # textless attachment admission is self-thread only (#162)
        if is_self_row:
            # Loop-proof echo filter (#161/#162): consume the durable send-record
            # first — by text, then by a sent file's transfer_name — then fall back
            # to the stateless bot-prefix skip for any un-recorded "🤖 " row. Either
            # way chief's own reply (or file send) never re-dispatches.
            async with self._session_factory() as session:
                consumed = bool(text) and await repo.take_send(
                    session, sender, text
                )
                if not consumed:
                    for att in atts_raw:
                        name = att.get("transfer_name")
                        if name and await repo.take_send(
                            session, sender, str(name)
                        ):
                            consumed = True
                            break
            if consumed or (text and text.startswith(BOT_PREFIX)):
                return
        async with self._session_factory() as session:
            contact = await get_contact(
                session, platform=PLATFORM, user_id=sender
            )
        if contact is None:
            # The whitelist is the event gate: no session, no reply, and only
            # handle + timestamp recorded — the text itself goes nowhere.
            async with self._session_factory() as session:
                await repo.record_unknown_sender(
                    session,
                    platform=PLATFORM,
                    handle=sender,
                    seen_at=self._row_time(row),
                )
            return
        if contact.tier == repo.TIER_OWNER:
            await self._on_owner(sender, text, atts_raw)
            return
        await self._on_guest(contact.display_name or sender, sender, text)

    @staticmethod
    def _row_time(row: dict[str, Any]) -> datetime:
        """The row's UTC wall clock, naive (the shape sqlite round-trips)."""
        try:
            return datetime.fromisoformat(str(row.get("timestamp")))
        except ValueError:
            return datetime.now(UTC).replace(tzinfo=None)

    async def _on_owner(
        self,
        sender: str,
        text: str,
        atts_raw: list[dict[str, Any]] | None = None,
    ) -> None:
        if text.startswith("/"):
            await self._on_command(sender, text)
            return
        attachments = await self._build_attachments(atts_raw or [])
        if not text and not attachments:
            return
        logger.info("owner message", extra={"thread_key": sender})
        await self._engine.dispatch(
            thread_key=sender, text=text, attachments=attachments, surface=Surface.DM
        )

    async def _build_attachments(
        self, atts_raw: Sequence[dict[str, Any]]
    ) -> tuple[Attachment, ...]:
        """Read each admitted attachment off the store's attachment path, converting
        HEIC/HEIF to JPEG for vision compatibility (#162); drops unsupported types
        and anything over the M8 size cap, matching the Telegram intake path
        exactly."""
        items: list[Attachment] = []
        for att in atts_raw:
            if len(items) >= MAX_ATTACHMENTS:
                break
            loaded = await self._load_attachment(att)
            if loaded is not None:
                items.append(loaded)
        return tuple(items)

    async def _load_attachment(self, att: dict[str, Any]) -> Attachment | None:
        mime = str(att.get("mime_type") or "")
        if not is_supported_media(mime):
            return None
        total_bytes = att.get("total_bytes")
        if isinstance(total_bytes, int) and total_bytes > MAX_ATTACHMENT_BYTES:
            return None
        raw_path = att.get("filename")
        if not raw_path:
            return None
        path = Path(str(raw_path)).expanduser()
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning("imessage attachment unreadable: %s", path)
            return None
        if len(data) > MAX_ATTACHMENT_BYTES:
            return None
        filename = str(att.get("transfer_name") or path.name)
        media_type = mime
        if mime.lower() in HEIC_MIME_TYPES:
            try:
                data = await self._convert_heic(path)
            except RuntimeError:
                logger.warning("HEIC conversion failed for %s", path)
                return None
            if len(data) > MAX_ATTACHMENT_BYTES:
                return None
            media_type = "image/jpeg"
            filename = Path(filename).stem + ".jpg"
        return Attachment(media_type=media_type, data=data, filename=filename)

    async def _convert_heic(self, source: Path) -> bytes:
        """Convert one HEIC/HEIF file to JPEG bytes via macOS ``sips`` (#162): the
        Messages store keeps the original on disk, so this shells out on that real
        path rather than round-tripping bytes through a temp source file."""
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "converted.jpg"
            result = await self._runner.run_sips(
                ["-s", "format", "jpeg", str(source), "--out", str(out_path)]
            )
            if not result.ok:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"sips HEIC conversion failed: {detail}")
            return out_path.read_bytes()

    async def _on_command(self, sender: str, text: str) -> None:
        """Dispatch an owner ``/command`` through the shared registry (#129)."""
        name, _, arg = text[1:].partition(" ")
        await self._commands.dispatch(
            name.lower(),
            CommandContext(
                engine=self._engine,
                memory=self._memory,
                thread_key=sender,
                arg=arg.strip(),
                is_casual=False,
                reply=self._reply_to(sender),
            ),
        )

    def _reply_to(self, handle: str) -> Callable[[str], Awaitable[None]]:
        async def reply(text: str) -> None:
            await self._io.send(handle, text)

        return reply

    async def _on_guest(self, label: str, sender: str, text: str) -> None:
        logger.info("guest message", extra={"thread_key": sender})
        if not self._guest_enabled or self._front_desk is None:
            await self._io.send(sender, self._guest_ack)
            return
        message = Message(
            platform=PLATFORM,
            sender_id=sender,
            text=text,
            thread_key=sender,
            tier=Tier.GUEST,
            sender_name=label,
            surface=Surface.DM,
        )
        await _handle_guest_message(
            message=message,
            io=self._io,
            engine=self._engine,
            session_factory=self._session_factory,
            reply=self._reply_to(sender),
            front_desk=self._front_desk,
            guest_ack=self._guest_ack,
            rate_limit=self._guest_rate,
            rate_window_seconds=self._guest_rate_window,
            global_limit=self._guest_global_rate,
            prompted=self._prompted_admission,
        )
