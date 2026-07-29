"""iMessage adapter over a fake chat.db: cursor, mapping, echo, send."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from chief.adapters.base import Message
from chief.adapters.imessage import BOT_PREFIX, IMessageAdapter
from chief.adapters.imessage_send import owner_send_guard
from chief.selfedit.recovery import RestartBoundary, RestartController

OWNER = "+15550001111"
CHIEF = "chief@example.com"  # chief's own Apple ID handle (dedicated mode)

# A real streamtyped attributedBody blob from a macOS chat.db (is_from_me=1,
# text column NULL). Decodes to "I’ll take the edi too" (curly apostrophe).
SELF_DM_BODY = bytes.fromhex(
    "040b73747265616d747970656481e803840140848484124e534174747269627574"
    "6564537472696e67008484084e534f626a656374008592848484084e5353747269"
    "6e67019484012b1749e280996c6c2074616b65207468652065646920746f6f8684"
    "0269490115928484840c4e5344696374696f6e617279009484016901928496961d"
    "5f5f6b494d4d657373616765506172744174747269627574654e616d6586928484"
    "84084e534e756d626572008484074e5356616c7565009484012a84999900868686"
)

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, handle_id INTEGER, text TEXT,
    is_from_me INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
    attributedBody BLOB, date INTEGER DEFAULT 0
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY, style INTEGER, room_name TEXT,
    chat_identifier TEXT
);
CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
"""


class FakeStore:
    """A chat.db lookalike the adapter polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)

    def add_message(
        self,
        sender: str,
        text: str,
        *,
        from_me: int = 0,
        tapback: int = 0,
        group: bool = False,
        chat: str | None = None,
        body: bytes | None = None,
        date: int = 0,
    ) -> None:
        """Insert one message, optionally in a direct chat (``chat`` =
        the chat_identifier) or a group. ``chat`` models the self-chat
        when it equals an owner handle; with ``group=True`` it names the
        room, so repeated calls land in one group thread. ``body`` sets
        attributedBody — how modern macOS stores the owner's own sends, with
        ``text`` left empty. ``date`` is the row's ns timestamp, used for
        twin dedup."""
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT ROWID FROM handle WHERE id = ?", (sender,)
            ).fetchone()
            handle_id = (
                row[0]
                if row
                else conn.execute(
                    "INSERT INTO handle (id) VALUES (?)", (sender,)
                ).lastrowid
            )
            msg_id = conn.execute(
                "INSERT INTO message (handle_id, text, is_from_me, "
                "associated_message_type, attributedBody, date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (handle_id, text or None, from_me, tapback, body, date),
            ).lastrowid
            chat_id: int | None = None
            if group and chat is None:
                chat = "group;+;room"
            if chat is not None:
                style, room = (43, "room") if group else (45, None)
                found = conn.execute(
                    "SELECT ROWID FROM chat WHERE chat_identifier = ?", (chat,)
                ).fetchone()
                chat_id = (
                    found[0]
                    if found
                    else conn.execute(
                        "INSERT INTO chat (style, room_name, chat_identifier) "
                        "VALUES (?, ?, ?)",
                        (style, room, chat),
                    ).lastrowid
                )
            if chat_id is not None:
                conn.execute(
                    "INSERT INTO chat_message_join (message_id, chat_id) "
                    "VALUES (?, ?)",
                    (msg_id, chat_id),
                )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.store = FakeStore(tmp_path / "chat.db")
        self.delivered: list[Message] = []
        self.jxa_calls: list[tuple[str, tuple[str, ...]]] = []
        self.cursor_path = tmp_path / "cursor"
        self.restart: RestartBoundary | None = None
        # Optional hook run inside on_message — a self-edit turn requesting a
        # restart is modelled by having it call ``controller.request()``.
        self.on_deliver: Callable[[Message], Awaitable[None]] | None = None
        # Optional poll-stage approval resolver (dispatcher.resolve_approval).
        self.resolve_approval: Callable[[Message], bool] | None = None
        # imessage.mode = dedicated: chief on its own Apple ID, so the four
        # self-DM compensations are off.
        self.dedicated = False
        # The owner's own chat.db, polled alongside chief's in dedicated mode.
        self.owner_store: FakeStore | None = None
        self.self_handles: tuple[str, ...] = ()

    def adapter(self) -> IMessageAdapter:
        async def on_message(message: Message) -> None:
            self.delivered.append(message)
            if self.on_deliver is not None:
                await self.on_deliver(message)

        async def run_jxa(script: str, argv: tuple[str, ...]) -> str:
            self.jxa_calls.append((script, argv))
            return "sent"

        return IMessageAdapter(
            on_message,
            db_path=self.store.path,
            cursor_path=self.cursor_path,
            owner_handles=(OWNER,),
            run_jxa=run_jxa,
            restart=self.restart,
            resolve_approval=self.resolve_approval,
            dedicated=self.dedicated,
            owner_db_path=(
                None if self.owner_store is None else self.owner_store.path
            ),
            self_handles=self.self_handles,
        )


async def test_first_boot_starts_at_head_no_replay(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "ancient history")
    adapter = harness.adapter()
    await adapter.start()
    await adapter.stop()
    harness.store.add_message(OWNER, "fresh text")
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["fresh text"]


async def test_owner_maps_stranger_passes_echo_and_noise_skip(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "hi chief")  # dedicated-mode inbound
    harness.store.add_message("+15559998888", "yo from a stranger")
    # chief's real echo: from_me=1, in the self-chat, 🤖-prefixed.
    harness.store.add_message(
        OWNER, BOT_PREFIX + "hi yourself", from_me=1, chat=OWNER
    )
    harness.store.add_message(OWNER, "loved a message", tapback=2000)
    # owner->friend sent copy: from_me=1, NOT the self-chat -> never dispatch.
    harness.store.add_message("+15551112222", "sent copy", from_me=1)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "hi chief"),
        ("+15559998888", "+15559998888", "yo from a stranger"),
    ]
    assert all(m.channel == "imessage" for m in harness.delivered)


async def test_group_message_threads_on_the_chat_not_the_sender(
    tmp_path: Path,
) -> None:
    """A group is the one place sender and thread_key diverge: many people
    share one conversation, so monitors scope on the chat identifier."""
    harness = Harness(tmp_path)
    harness.store.add_message("+15557776666", "group chatter", group=True)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("+15557776666", "group;+;room", "group chatter")
    ]


async def test_group_participants_share_one_thread_key(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message("+15557776666", "one", group=True)
    harness.store.add_message("+15554443333", "two", group=True)
    harness.store.add_message("+15557776666", "three", group=True, chat="other")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key) for m in harness.delivered] == [
        ("+15557776666", "group;+;room"),
        ("+15554443333", "group;+;room"),
        ("+15557776666", "other"),
    ]


async def test_group_sender_is_never_mapped_to_owner(tmp_path: Path) -> None:
    """Even when a group message carries an owner handle, it maps to the raw
    sender. Sender ``owner`` takes the dispatcher's owner path — a turn, then
    a reply to thread_key — and thread_key here is a group the one-to-one
    send path cannot address. Groups must stay on the stranger path."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "owner talking in a group", group=True)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key) for m in harness.delivered] == [
        (OWNER, "group;+;room")
    ]


async def test_chief_own_group_send_never_polls_back(tmp_path: Path) -> None:
    """Chief texts a group out-of-band via the imsg CLI. That row lands
    is_from_me=1 outside the owner self-chat, so the scope predicate drops
    it — no echo loop, and no BOT_PREFIX needed in a group."""
    harness = Harness(tmp_path)
    harness.store.add_message(
        "+15557776666", "sent to the group", from_me=1, group=True
    )
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert harness.delivered == []


async def test_owner_self_dm_from_me_delivered(tmp_path: Path) -> None:
    """Same-account self-DM: the owner texts their own self-chat, so the
    row is from_me=1 yet must run a turn as sender 'owner'."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "note to self", from_me=1, chat=OWNER)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "note to self"),
    ]


async def test_self_dm_body_in_attributedbody_is_decoded(tmp_path: Path) -> None:
    """The owner's own sends store their text only in attributedBody (text
    NULL). A real self-DM blob must decode and dispatch, not get dropped."""
    harness = Harness(tmp_path)
    harness.store.add_message(
        OWNER, "", from_me=1, chat=OWNER, body=SELF_DM_BODY
    )
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.text) for m in harness.delivered] == [
        ("owner", "I’ll take the edi too"),
    ]


async def test_owner_self_dm_twin_delivered_once(tmp_path: Path) -> None:
    """macOS records one owner self-DM as a twin: an is_from_me=1 row and an
    is_from_me=0 row, same text, same self-chat, same instant (different guids).
    Only one turn may run — else chief answers the one message twice."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "ping", from_me=0, chat=OWNER, date=100)
    harness.store.add_message(OWNER, "ping", from_me=1, chat=OWNER, date=100)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.text) for m in harness.delivered] == [("owner", "ping")]


async def test_owner_repeats_text_after_window_delivers_both(
    tmp_path: Path,
) -> None:
    """Dedup is time-bounded: the owner legitimately sending the same text
    again well after the twin window must still run a second turn."""
    harness = Harness(tmp_path)
    later = 10_000_000_000  # 10s in ns, past the dedup window
    harness.store.add_message(OWNER, "ok", from_me=1, chat=OWNER, date=0)
    harness.store.add_message(OWNER, "ok", from_me=1, chat=OWNER, date=later)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["ok", "ok"]


async def test_self_chat_scope_does_not_leak_other_conversations(
    tmp_path: Path,
) -> None:
    """A from_me=1 message in a NON-self chat (owner texting a friend) must
    never dispatch, even though a self-chat from_me=1 in the same poll does."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "self note", from_me=1, chat=OWNER)
    harness.store.add_message(
        "+15554443333", "hey friend", from_me=1, chat="+15554443333"
    )
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.text) for m in harness.delivered] == [
        ("owner", "self note"),
    ]


# --- dedicated mode: chief on its own Apple ID ----------------------------


async def test_dedicated_mode_round_trip_reply_does_not_re_enter(
    tmp_path: Path,
) -> None:
    """The central mechanism. On its own Apple ID chief's chat with the owner
    is an ordinary conversation whose chat_identifier IS the owner's handle:
    the owner's texts arrive is_from_me=0, chief's replies are is_from_me=1 in
    that same chat. So the self-chat scope must be OFF, not repointed at the
    owner — repointed, every reply below would poll straight back as owner
    input and loop. Driven through the real query against a real store."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.store.add_message(OWNER, "hi chief", from_me=0, chat=OWNER)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [(m.sender, m.thread_key, m.text) for m in harness.delivered] == [
        ("owner", OWNER, "hi chief"),
    ]

    await adapter.send(OWNER, "hello back")
    assert harness.jxa_calls[0][1] == (OWNER, "hello back")  # no BOT_PREFIX

    # Chief's own reply as its store records it, then a second poll.
    harness.store.add_message(OWNER, "hello back", from_me=1, chat=OWNER)
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["hi chief"]


async def test_dedicated_mode_drops_the_self_dm_compensations(
    tmp_path: Path,
) -> None:
    """Prefix filter and twin dedup are self-DM artefacts: in dedicated mode a
    real owner message that happens to start with 🤖 must run a turn, and two
    quick identical texts are two messages, not one row recorded twice."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.store.add_message(OWNER, BOT_PREFIX + "robot emoji", chat=OWNER)
    harness.store.add_message(OWNER, "ok", chat=OWNER, date=100)
    harness.store.add_message(OWNER, "ok", chat=OWNER, date=100)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == [
        BOT_PREFIX + "robot emoji", "ok", "ok",
    ]


async def test_dedicated_mode_still_ignores_owner_sends_to_others(
    tmp_path: Path,
) -> None:
    """Chief reads the owner's store too (monitors), so its own poll sees the
    owner's outbound copies. Those are not messages to chief."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.store.add_message(
        "+15554443333", "hey friend", from_me=1, chat="+15554443333"
    )
    harness.store.add_message("+15559998888", "yo from a stranger")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["yo from a stranger"]


async def test_cursor_persists_across_restarts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "first")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert len(harness.delivered) == 1

    reborn = harness.adapter()
    await reborn.start()
    await reborn.stop()
    harness.store.add_message(OWNER, "second")
    await reborn.poll_once()
    await reborn.drain()
    assert [m.text for m in harness.delivered] == ["first", "second"]


async def test_selfedit_row_saves_cursor_before_restart_no_double_send(
    tmp_path: Path,
) -> None:
    """A self-edit row execs a restart. The cursor must be persisted BEFORE the
    execv, so re-polling after the (fake) restart does NOT re-deliver the row —
    otherwise chief answers the same message a second time (the double-send)."""
    harness = Harness(tmp_path)
    cursor_at_restart: list[str] = []
    controller = RestartController(
        lambda: cursor_at_restart.append(harness.cursor_path.read_text())
    )
    harness.restart = controller

    async def request_restart(message: Message) -> None:
        controller.request()

    harness.on_deliver = request_restart
    harness.store.add_message(OWNER, "self-edit please", from_me=1, chat=OWNER)

    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()

    # Delivered once; the cursor was already durable when the restart fired.
    assert [m.text for m in harness.delivered] == ["self-edit please"]
    assert cursor_at_restart == [harness.cursor_path.read_text()]
    assert harness.cursor_path.read_text() != "0"

    # A fresh adapter (the reboot) resumes from the saved cursor: no re-poll.
    harness.restart = None
    harness.on_deliver = None
    reborn = harness.adapter()
    await reborn.start()
    await reborn.stop()
    await reborn.poll_once()
    await reborn.drain()
    assert [m.text for m in harness.delivered] == ["self-edit please"]


async def test_slow_thread_does_not_block_other_thread(tmp_path: Path) -> None:
    """A slow turn on one thread must not stall another thread's turn. Thread A
    blocks on a gate; thread B runs to completion and opens the gate — so B
    finishes before A, proving the two workers run concurrently."""
    harness = Harness(tmp_path)
    gate = asyncio.Event()
    order: list[str] = []

    async def on_deliver(message: Message) -> None:
        if message.thread_key == OWNER:
            await gate.wait()  # thread A stalls until B opens the gate
            order.append("A")
        else:
            order.append("B")
            gate.set()

    harness.on_deliver = on_deliver
    harness.store.add_message(OWNER, "slow", from_me=1, chat=OWNER)
    harness.store.add_message("+15559998888", "fast")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert order == ["B", "A"]


async def test_same_thread_runs_in_arrival_order(tmp_path: Path) -> None:
    """Two messages on one thread run serially in arrival order (single FIFO
    worker per thread)."""
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "first", from_me=1, chat=OWNER, date=1)
    harness.store.add_message(OWNER, "second", from_me=1, chat=OWNER, date=2)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["first", "second"]


async def test_approval_answer_bypasses_blocked_worker(tmp_path: Path) -> None:
    """A gated turn suspends its thread's FIFO worker awaiting the owner's
    approval. The answer arrives on the SAME thread — it must resolve at the
    poll stage (bypassing the busy worker), not queue behind the very turn it
    unblocks. Otherwise the thread deadlocks until the card times out."""
    harness = Harness(tmp_path)
    release = asyncio.Event()
    resolved: list[str] = []

    async def on_deliver(message: Message) -> None:
        if message.text == "run the tool":
            await release.wait()  # gated turn suspended awaiting approval

    def resolve_approval(message: Message) -> bool:
        if message.text == "yes":
            resolved.append(message.thread_key)
            release.set()  # the answer unblocks the suspended turn
            return True
        return False

    harness.on_deliver = on_deliver
    harness.resolve_approval = resolve_approval
    harness.store.add_message(OWNER, "run the tool", from_me=1, chat=OWNER, date=1)
    adapter = harness.adapter()
    await adapter.poll_once()  # enqueues the gated turn; worker now blocked

    harness.store.add_message(OWNER, "yes", from_me=1, chat=OWNER, date=2)
    # The answer resolves even though the worker is stuck on the earlier turn.
    await asyncio.wait_for(adapter.poll_once(), timeout=1.0)
    assert resolved == [OWNER]
    # The answer bypassed the worker (never delivered as a turn), and the
    # unblocked gated turn now completes.
    await asyncio.wait_for(adapter.drain(), timeout=1.0)
    assert [m.text for m in harness.delivered] == ["run the tool"]
    await adapter.stop()


async def test_non_answer_still_runs_a_turn(tmp_path: Path) -> None:
    """When no card is pending the resolver returns False, so the message
    routes to its worker and runs a turn as normal."""
    harness = Harness(tmp_path)
    harness.resolve_approval = lambda message: False
    harness.store.add_message(OWNER, "just chatting", from_me=1, chat=OWNER)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["just chatting"]


async def test_poll_once_returns_without_awaiting_turn(tmp_path: Path) -> None:
    """poll_once enqueues and returns even when a turn never completes — the
    hung turn detaches onto its worker rather than stalling the poll loop."""
    harness = Harness(tmp_path)
    started = asyncio.Event()

    async def on_deliver(message: Message) -> None:
        started.set()
        await asyncio.Event().wait()  # never returns

    harness.on_deliver = on_deliver
    harness.store.add_message(OWNER, "hang", from_me=1, chat=OWNER)
    adapter = harness.adapter()
    await asyncio.wait_for(adapter.poll_once(), timeout=1.0)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await adapter.stop()  # cancels the hung worker


async def test_send_prefixes_owner_threads_only(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    adapter = harness.adapter()
    await adapter.send(OWNER, "reply to self-chat")
    await adapter.send("+15559998888", "owner-requested text")
    assert harness.jxa_calls[0][1] == (OWNER, BOT_PREFIX + "reply to self-chat")
    assert harness.jxa_calls[1][1] == ("+15559998888", "owner-requested text")


# --- owner_send_guard: the mechanical echo-loop seatbelt (audit C1) ---------


def test_owner_send_guard_blocks_imsg_to_owner() -> None:
    guard = owner_send_guard(("+16505550000",))
    refusal = guard('imsg send --to "+16505550000" --text "hi"')
    assert refusal is not None
    assert "self-reply loop" in refusal


def test_owner_send_guard_blocks_bare_digits_and_osascript() -> None:
    guard = owner_send_guard(("+16505550000",))
    assert guard("imsg send --to 16505550000 --text hi") is not None
    assert guard('osascript -e \'send "x" to buddy "16505550000"\'') is not None


def test_owner_send_guard_allows_other_recipients() -> None:
    guard = owner_send_guard(("+16505550000",))
    assert guard('imsg send --to "+15559998888" --text "for a friend"') is None
    assert guard("imsg chats --limit 5") is None
    assert guard("imsg history --chat-id 7 --limit 5") is None


def test_owner_send_guard_ignores_non_messaging_commands() -> None:
    # The handle alone (e.g. in a grep over logs) is fine — only commands that
    # also invoke an out-of-band sender are blocked.
    guard = owner_send_guard(("+16505550000",))
    assert guard("grep 16505550000 data/chief.log") is None
    assert guard("ls -la") is None


def test_owner_send_guard_no_handles_is_noop() -> None:
    assert owner_send_guard(())("imsg send --to x --text y") is None


def test_owner_send_guard_email_handle_matches_case_insensitively() -> None:
    # Apple-ID handles are emails and case-insensitive; a case-mixed config
    # handle must still block a lowercased send (issue #233).
    guard = owner_send_guard(("Owner@iCloud.com",))
    assert guard("imsg send --to owner@icloud.com --text hi") is not None
    assert guard("IMSG send --to OWNER@ICLOUD.COM --text hi") is not None
    assert guard("imsg send --to friend@icloud.com --text hi") is None


# --- dual-store reach: chief's own store plus the owner's (#286)


async def test_dedicated_mode_polls_both_stores(tmp_path: Path) -> None:
    """Chief keeps reading the owner's store so their existing monitors keep
    working — the documented reach of the new account boundary."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.owner_store = FakeStore(tmp_path / "owner.db")
    harness.store.add_message(OWNER, "hi chief", chat=OWNER)
    harness.owner_store.add_message("+15559998888", "a friend texts the owner")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == [
        "hi chief",
        "a friend texts the owner",
    ]


async def test_chiefs_own_reply_in_the_owners_store_never_polls_back(
    tmp_path: Path,
) -> None:
    """The trap in dual-store reach: chief's reply lands in the OWNER's store
    as an ordinary is_from_me = 0 row from chief's handle. Polling it would
    re-create the echo loop by another route, with no BOT_PREFIX left to catch
    it — dedicated mode turned that off."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.self_handles = (CHIEF,)
    harness.owner_store = FakeStore(tmp_path / "owner.db")
    harness.owner_store.add_message(CHIEF, "hello back", chat=CHIEF)
    harness.owner_store.add_message("+15559998888", "a friend texts the owner")
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["a friend texts the owner"]


async def test_each_store_keeps_its_own_cursor(tmp_path: Path) -> None:
    """Rowids are per-store: one shared cursor would skip whichever store is
    behind, silently dropping messages."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.owner_store = FakeStore(tmp_path / "owner.db")
    for i in range(5):
        harness.owner_store.add_message("+15559998888", f"owner-side {i}")
    harness.store.add_message(OWNER, "to chief", chat=OWNER)
    adapter = harness.adapter()
    await adapter.poll_once()
    await adapter.drain()
    assert len(harness.delivered) == 6
    # Across a restart both positions come back off disk. A shared cursor file
    # would carry the owner store's much higher rowid into chief's store and
    # swallow everything below it.
    harness.store.add_message(OWNER, "second", chat=OWNER)
    restarted = harness.adapter()
    await restarted.start()
    await restarted.poll_once()
    await restarted.drain()
    await restarted.stop()
    assert [m.text for m in harness.delivered][-1:] == ["second"]


async def test_an_unreadable_owner_store_costs_that_store_not_the_daemon(
    tmp_path: Path,
) -> None:
    """Reading the owner's store needs a per-user Full Disk Access grant and a
    readable ~/Library/Messages. Missing either must not take `Daemon.start`
    down — the web UI is the other half of the boot check that would report
    it."""
    harness = Harness(tmp_path)
    harness.dedicated = True
    harness.owner_store = FakeStore(tmp_path / "owner.db")
    harness.owner_store.path.write_bytes(b"not a database at all")
    adapter = harness.adapter()
    # start() then stop() before polling by hand: start() leaves the poll loop
    # running, and its first tick would race an explicit poll_once() over the
    # same rows (both fetch before either advances the cursor).
    await adapter.start()
    await adapter.stop()
    harness.store.add_message(OWNER, "hi chief", chat=OWNER)
    await adapter.poll_once()
    await adapter.drain()
    assert [m.text for m in harness.delivered] == ["hi chief"]


async def test_chiefs_own_unreadable_store_still_fails_loudly(
    tmp_path: Path,
) -> None:
    """Degrading is only right for the store chief merely reaches into. Losing
    its own is the whole channel, and must not boot to a silent no-op."""
    harness = Harness(tmp_path)
    harness.store.path.write_bytes(b"not a database at all")
    with pytest.raises(sqlite3.DatabaseError):
        await harness.adapter().start()


async def test_one_store_when_no_owner_store_is_configured(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.store.add_message(OWNER, "hi", chat=OWNER)
    adapter = harness.adapter()
    await adapter.start()
    await adapter.stop()
    assert not (tmp_path / "cursor.owner").exists()
