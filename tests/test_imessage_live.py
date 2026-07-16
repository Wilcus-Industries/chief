"""Opt-in live iMessage tests (#156): both real boundaries on the owner's Mac.

Skipped unless ``CHIEF_IMESSAGE_LIVE`` is set AND the platform is darwin — the
``CHIEF_APPLE_LIVE`` pattern. This is where the PRD's central mechanism runs for
real: the production poll SQL against the genuine live Messages store (Full Disk
Access) and a real AppleScript send through Messages (Automation), round-trip
asserted by reading the sent row back out of the store. It also **re-validates the
CI fixture's schema** against what macOS actually writes, so the fake store can't
drift (the live suite is the canary for Apple breaking store or scripting
assumptions across releases).

Run on the Mac test rig, with the doctor green first::

    CHIEF_IMESSAGE_LIVE=1 CHIEF_IMESSAGE_TEST_HANDLE=+1555... \
        uv run pytest tests/test_imessage_live.py

``CHIEF_IMESSAGE_TEST_HANDLE`` (an iMessage-reachable handle the machine may text —
the owner's own number) is required by the send round-trip test; the read-only
tests run without it. Prereqs: Full Disk Access + Automation → Messages for the
terminal running the tests, and a signed-in Messages account (the dedicated
Apple ID).

**Self-DM e2e gate (#164)** — the additional ``CHIEF_IMESSAGE_E2E=1`` tests put
the *running daemon* in the loop: chief must be up on this machine in self-DM
mode (``imessage_enabled`` + ``imessage_self_dm`` + the owner handles — the
setup walk-through in DESIGN.md), with ``CHIEF_IMESSAGE_TEST_HANDLE`` set to the
owner's own number. Each test plays the phone: a raw un-prefixed JXA send into
the self-thread is byte-identical to a phone self-text (chief's echo filter only
consumes 🤖-prefixed or send-recorded rows), then the store is polled for the
daemon's 🤖 reply. These burn real model turns::

    CHIEF_IMESSAGE_LIVE=1 CHIEF_IMESSAGE_E2E=1 \
        CHIEF_IMESSAGE_TEST_HANDLE=+1555... \
        uv run pytest tests/test_imessage_live.py -s

Note: with the self-DM daemon live, the plain round-trip test's un-prefixed
marker also provokes one (harmless) chief reply — expected chatter.

The non-self silence test additionally needs ``CHIEF_IMESSAGE_SECOND_HANDLE``
(a non-owner handle, exactly as the store spells it, with **no active watch**
bound to it), a human ready to text from that device when prompted (hence
``-s``), and ``CHIEF_DB_PATH`` if chief's own sqlite is not at the default
``~/.local/share/chief/data/chief.db``.

**Watches e2e gate (#171)** — the ``test_watch_*`` / ``test_nonwatched_*``
cases prove PRD #160's central mechanism with the same envs and the same human
on the second device: a watch set *conversationally* in the self-thread, the
real engine judging a real trigger text's relevance, a real Messages send
landing at the second handle, and the fire-report (or, silent-tone, its
absence) in the self-thread — plus the send seam refusing a non-watched text.
They run in definition order and leave no armed watch behind; each prints what
to text from the second device and when.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

from chief.adapters.imessage import (
    BOT_PREFIX,
    SEND_FILE_SCRIPT,
    SEND_TEXT_SCRIPT,
    IMessageTaskIO,
    build_head_query,
    build_poll_query,
)
from chief.persistence.imessage import PLATFORM
from chief.tools.apple.doctor import probe_all
from chief.tools.apple.runner import ScriptRunner
from imessage_helpers import CHAT_DB_SCHEMA, solid_png, tiny_pdf

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_IMESSAGE_LIVE") or sys.platform != "darwin",
    reason="live iMessage test — set CHIEF_IMESSAGE_LIVE=1 on a Mac with the "
    "TCC grants",
)

LIVE_TIMEOUT = pytest.mark.timeout(120)

CHAT_DB = str(Path.home() / "Library/Messages/chat.db")

#: The handle the round-trip test may really text (the owner's own number).
TEST_HANDLE = os.environ.get("CHIEF_IMESSAGE_TEST_HANDLE", "")


def _runner() -> ScriptRunner:
    return ScriptRunner(timeout=60.0)


def _fixture_columns() -> dict[str, set[str]]:
    """table → columns the CI fixture declares (parsed from its schema DDL)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(CHAT_DB_SCHEMA)
    tables = {
        "handle",
        "chat",
        "message",
        "chat_message_join",
        "attachment",
        "message_attachment_join",
    }
    return {
        table: {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for table in tables
    }


@LIVE_TIMEOUT
async def test_fixture_schema_matches_the_live_store() -> None:
    """Every table/column the CI fixture models must exist in the real chat.db."""
    runner = _runner()
    for table, columns in _fixture_columns().items():
        result = await runner.run_sqlite(
            CHAT_DB, f"PRAGMA table_info({table});"
        )
        assert result.ok, result.stderr
        live_columns = {
            str(row["name"]) for row in json.loads(result.stdout or "[]")
        }
        missing = columns - live_columns
        assert not missing, (
            f"fixture drift: {table} columns {missing} are not in the live store"
        )


@LIVE_TIMEOUT
async def test_poll_queries_run_against_the_live_store() -> None:
    """The production SQL itself is schema-valid on the genuine store."""
    runner = _runner()
    head = await runner.run_sqlite(CHAT_DB, build_head_query())
    assert head.ok, head.stderr
    rows = json.loads(head.stdout or "[]")
    head_rowid = int(rows[0]["head"]) if rows else 0

    # Poll from a bit behind the head so the query exercises real rows.
    poll = await runner.run_sqlite(
        CHAT_DB, build_poll_query(max(0, head_rowid - 50))
    )
    assert poll.ok, poll.stderr
    for row in json.loads(poll.stdout or "[]"):
        assert {"rowid", "sender", "text", "timestamp", "in_group",
                "has_room"} <= set(row)


@LIVE_TIMEOUT
async def test_doctor_probes_both_imessage_capabilities() -> None:
    health = {
        h.capability: h
        for h in await probe_all(_runner(), messages_db_path=CHAT_DB)
    }
    assert "messages" in health and "messages_send" in health
    for capability in ("messages", "messages_send"):
        item = health[capability]
        assert item.ok, f"{capability}: {item.status} — {item.detail}\n{item.fix}"


@LIVE_TIMEOUT
@pytest.mark.skipif(
    not TEST_HANDLE,
    reason="set CHIEF_IMESSAGE_TEST_HANDLE to a handle this Mac may text",
)
async def test_send_round_trip_lands_in_the_store(tmp_path: Path) -> None:
    """A real send, asserted by reading the marker back out of the store.

    In a self-chat (the handle is the account's own number) the *sent* copy's
    ``text`` is often NULL and the marker text lives on the pair's received copy
    (#164 rig finding), so the read-back matches the marker on either copy
    rather than requiring ``is_from_me = 1``.
    """
    marker = f"chief live-test {uuid.uuid4().hex[:8]}"
    io = IMessageTaskIO(_runner(), outbox_dir=str(tmp_path / "outbox"))

    await io.send(TEST_HANDLE, marker)

    runner = _runner()
    for _ in range(30):  # Messages writes the store asynchronously
        result = await runner.run_sqlite(
            CHAT_DB,
            "SELECT text, is_from_me FROM message "
            f"WHERE text = '{marker}' LIMIT 1;",
        )
        assert result.ok, result.stderr
        if json.loads(result.stdout or "[]"):
            return
        await asyncio.sleep(1)
    raise AssertionError("sent message never appeared in the live store")


# --- Self-DM e2e gate (#164): the running daemon is in the loop ----------------

E2E = pytest.mark.skipif(
    not os.environ.get("CHIEF_IMESSAGE_E2E"),
    reason="daemon-in-the-loop e2e — set CHIEF_IMESSAGE_E2E=1 with chief running "
    "on this machine in self-DM mode",
)

NEEDS_HANDLE = pytest.mark.skipif(
    not TEST_HANDLE,
    reason="set CHIEF_IMESSAGE_TEST_HANDLE to the owner's own number",
)

#: Generous ceiling for a full model turn (dispatch → engine → JXA send → store).
REPLY_TIMEOUT = 240.0

#: How long after a reply to keep watching for loop/double-answer symptoms —
#: many multiples of the daemon's poll interval (default 2s), so a reply-to-echo
#: loop or a second dispatch would have fired well within it.
LOOP_GRACE = 20.0

#: A non-owner handle a human can text from, spelled exactly as the store spells
#: it; drives the inert-others proof. No active watch may be bound to it.
SECOND_HANDLE = os.environ.get("CHIEF_IMESSAGE_SECOND_HANDLE", "")

#: chief's own sqlite (unknown_senders lives there), read-only.
CHIEF_DB = os.environ.get(
    "CHIEF_DB_PATH", str(Path.home() / ".local/share/chief/data/chief.db")
)

E2E_TIMEOUT = pytest.mark.timeout(600)


def _sq(text: str) -> str:
    """Single-quote-escape one operator-supplied SQL string literal."""
    return text.replace("'", "''")


async def _store_head(runner: ScriptRunner) -> int:
    result = await runner.run_sqlite(CHAT_DB, build_head_query())
    assert result.ok, result.stderr
    rows = json.loads(result.stdout or "[]")
    return int(rows[0]["head"]) if rows else 0


async def _bot_echo_rows(
    runner: ScriptRunner, after_rowid: int, pattern: str
) -> list[dict[str, object]]:
    """Received (``is_from_me = 0``) echo copies of chief's own 🤖 sends.

    The daemon's replies come back as received rows too (the self-chat pair);
    unlike the sent copies their ``text`` is reliably populated, so assertions
    key on them. ``pattern`` is matched in Python — no text reaches the SQL.
    """
    result = await runner.run_sqlite(
        CHAT_DB,
        "SELECT message.ROWID AS rowid, message.text AS text FROM message "
        f"WHERE message.ROWID > {int(after_rowid)} AND message.is_from_me = 0 "
        "AND message.text IS NOT NULL ORDER BY message.ROWID ASC;",
    )
    assert result.ok, result.stderr
    return [
        row
        for row in json.loads(result.stdout or "[]")
        if str(row["text"]).startswith(BOT_PREFIX)
        and re.search(pattern, str(row["text"]), re.IGNORECASE)
    ]


async def _await_bot_echo(
    runner: ScriptRunner, after_rowid: int, pattern: str
) -> list[dict[str, object]]:
    """Poll until at least one matching 🤖 reply lands, or time out."""
    deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        rows = await _bot_echo_rows(runner, after_rowid, pattern)
        if rows:
            return rows
        await asyncio.sleep(2)
    raise AssertionError(
        f"no 🤖 reply matching {pattern!r} within {REPLY_TIMEOUT:.0f}s — is the "
        "daemon running in self-DM mode?"
    )


async def _send_as_phone(runner: ScriptRunner, text: str) -> None:
    """A raw, un-prefixed, un-recorded self-send — what a phone self-text is."""
    result = await runner.run_jxa(SEND_TEXT_SCRIPT, [TEST_HANDLE, text])
    assert result.ok, result.stderr


@E2E_TIMEOUT
@E2E
@NEEDS_HANDLE
async def test_self_text_yields_exactly_one_bot_reply_and_no_loop() -> None:
    """#164 AC 1: self-text → one 🤖 reply in-thread; no double answer; chief's
    own reply provokes nothing further (loop-proof, observed live)."""
    runner = _runner()
    head = await _store_head(runner)
    token = f"ACK-{uuid.uuid4().hex[:8]}"

    await _send_as_phone(
        runner,
        f"Live-gate check: reply with exactly the word {token} and nothing else.",
    )

    await _await_bot_echo(runner, head, re.escape(token))
    await asyncio.sleep(LOOP_GRACE)
    rows = await _bot_echo_rows(runner, head, re.escape(token))
    assert len(rows) == 1, [row["text"] for row in rows]


@E2E_TIMEOUT
@E2E
@NEEDS_HANDLE
async def test_self_photo_yields_content_aware_reply(tmp_path: Path) -> None:
    """#164 AC 2a: a photo to self → a vision-grounded answer (names the color)."""
    runner = _runner()
    head = await _store_head(runner)
    token = uuid.uuid4().hex[:8]
    path = tmp_path / f"e2e-{token}.png"
    path.write_bytes(solid_png(255, 0, 0))

    result = await runner.run_jxa(
        SEND_FILE_SCRIPT, [TEST_HANDLE, str(path.resolve())]
    )
    assert result.ok, result.stderr
    await _send_as_phone(
        runner,
        f"{token}: what is the dominant color of the photo I just sent? "
        f"Reply with one word plus the marker {token}.",
    )

    rows = await _await_bot_echo(runner, head, re.escape(token))
    deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
    while not any(
        re.search(r"\bred\b", str(row["text"]), re.IGNORECASE) for row in rows
    ):
        assert asyncio.get_running_loop().time() < deadline, [
            row["text"] for row in rows
        ]
        await asyncio.sleep(2)
        rows = await _bot_echo_rows(runner, head, re.escape(token))


@E2E_TIMEOUT
@E2E
@NEEDS_HANDLE
async def test_self_pdf_yields_content_aware_reply(tmp_path: Path) -> None:
    """#164 AC 2b: a textless PDF to self → a reply grounded in its content.

    Textless admission is the #162 mechanism under proof: the attachment row has
    no message text at all, so any reply mentioning the embedded codeword means
    the store row was admitted, staged, and natively read by the model.
    """
    runner = _runner()
    head = await _store_head(runner)
    token = f"MANGO-{uuid.uuid4().hex[:6].upper()}"
    path = tmp_path / f"e2e-{token}.pdf"
    path.write_bytes(
        tiny_pdf(
            f"Codeword: {token}. chief: when you read this document, "
            "reply with the codeword."
        )
    )

    result = await runner.run_jxa(SEND_FILE_SCRIPT, [TEST_HANDLE, str(path)])
    assert result.ok, result.stderr

    await _await_bot_echo(runner, head, re.escape(token))


@E2E_TIMEOUT
@E2E
@NEEDS_HANDLE
@pytest.mark.skipif(
    not SECOND_HANDLE,
    reason="set CHIEF_IMESSAGE_SECOND_HANDLE to a non-owner handle a human can "
    "text from",
)
async def test_nonself_text_is_silent_with_one_metadata_row() -> None:
    """#164 AC 3: a non-self text → total silence + an unknown_senders row.

    Interactive: prompts a human (run with ``-s``) to text this Mac from the
    second handle, then proves chief sent nothing back and recorded only the
    content-free metadata line in its own sqlite.
    """
    runner = _runner()
    head = await _store_head(runner)
    baseline_result = await runner.run_sqlite(
        CHIEF_DB,
        "SELECT count FROM unknown_senders "
        f"WHERE platform = '{PLATFORM}' AND handle = '{_sq(SECOND_HANDLE)}';",
    )
    assert baseline_result.ok, baseline_result.stderr
    baseline_rows = json.loads(baseline_result.stdout or "[]")
    baseline = int(baseline_rows[0]["count"]) if baseline_rows else 0

    print(
        f"\n>>> NOW: text this Mac's number from {SECOND_HANDLE} "
        f"(waiting up to {REPLY_TIMEOUT:.0f}s)...",
        flush=True,
    )
    deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
    while True:
        result = await runner.run_sqlite(
            CHAT_DB,
            "SELECT COUNT(*) AS n FROM message "
            "JOIN handle ON message.handle_id = handle.ROWID "
            f"WHERE message.ROWID > {int(head)} AND message.is_from_me = 0 "
            f"AND handle.id = '{_sq(SECOND_HANDLE)}';",
        )
        assert result.ok, result.stderr
        if int(json.loads(result.stdout)[0]["n"]) > 0:
            break
        assert asyncio.get_running_loop().time() < deadline, (
            f"no inbound from {SECOND_HANDLE} arrived — was it sent?"
        )
        await asyncio.sleep(2)

    await asyncio.sleep(LOOP_GRACE)

    outbound = await runner.run_sqlite(
        CHAT_DB,
        "SELECT COUNT(*) AS n FROM message "
        "JOIN handle ON message.handle_id = handle.ROWID "
        f"WHERE message.ROWID > {int(head)} AND message.is_from_me = 1 "
        f"AND handle.id = '{_sq(SECOND_HANDLE)}';",
    )
    assert outbound.ok, outbound.stderr
    assert int(json.loads(outbound.stdout)[0]["n"]) == 0, (
        "chief replied to a non-self sender"
    )

    recorded = await runner.run_sqlite(
        CHIEF_DB,
        "SELECT count FROM unknown_senders "
        f"WHERE platform = '{PLATFORM}' AND handle = '{_sq(SECOND_HANDLE)}';",
    )
    assert recorded.ok, recorded.stderr
    recorded_rows = json.loads(recorded.stdout or "[]")
    assert recorded_rows, "no unknown_senders metadata row was written"
    assert int(recorded_rows[0]["count"]) > baseline


# --- Watches e2e gate (#171): real second-handle scenario ------------------------

NEEDS_SECOND = pytest.mark.skipif(
    not SECOND_HANDLE,
    reason="set CHIEF_IMESSAGE_SECOND_HANDLE to a non-owner handle a human can "
    "text from",
)

WATCH_TIMEOUT = pytest.mark.timeout(900)


def _norm_second() -> str:
    from chief.persistence.imessage import normalize_handle

    return normalize_handle(SECOND_HANDLE)


async def _watch_states(runner: ScriptRunner) -> list[dict[str, object]]:
    """All watches bound to the second handle, from chief's own sqlite."""
    result = await runner.run_sqlite(
        CHIEF_DB,
        "SELECT id, state, tone FROM watches "
        f"WHERE target_handle = '{_sq(_norm_second())}' ORDER BY id;",
    )
    assert result.ok, result.stderr
    return list(json.loads(result.stdout or "[]"))


async def _await_watch_state(
    runner: ScriptRunner, state: str, *, tone: str | None = None
) -> dict[str, object]:
    """Poll until a watch on the second handle reaches ``state`` (newest wins)."""
    deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        for row in reversed(await _watch_states(runner)):
            if row["state"] == state and (tone is None or row["tone"] == tone):
                return row
        await asyncio.sleep(2)
    raise AssertionError(
        f"no watch on {SECOND_HANDLE} reached state {state!r} within "
        f"{REPLY_TIMEOUT:.0f}s"
    )


async def _outbound_to_second(
    runner: ScriptRunner, after_rowid: int, pattern: str | None = None
) -> list[dict[str, object]]:
    """Real sends that landed at the second handle after ``after_rowid``."""
    result = await runner.run_sqlite(
        CHAT_DB,
        "SELECT message.ROWID AS rowid, message.text AS text FROM message "
        "JOIN handle ON message.handle_id = handle.ROWID "
        f"WHERE message.ROWID > {int(after_rowid)} AND message.is_from_me = 1 "
        f"AND handle.id = '{_sq(SECOND_HANDLE)}' ORDER BY message.ROWID ASC;",
    )
    assert result.ok, result.stderr
    rows = json.loads(result.stdout or "[]")
    if pattern is None:
        return list(rows)
    return [
        row
        for row in rows
        if row["text"] and re.search(pattern, str(row["text"]), re.IGNORECASE)
    ]


async def _await_outbound_to_second(
    runner: ScriptRunner, after_rowid: int, pattern: str | None = None
) -> None:
    """Wait until a real send lands at the second handle.

    Asserted by row EXISTENCE, not text: when the second handle is an alias of
    the owner's own Apple ID (the common single-account rig), the sent copy pairs
    like a self-chat and its ``text`` is NULL, so the ghost-send's content can't
    be read back off the outbound row. The exact reply content is proven instead
    by the self-thread fire-report (report tone) — see the report-tone test.
    """
    deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if await _outbound_to_second(runner, after_rowid, pattern):
            return
        await asyncio.sleep(2)
    raise AssertionError(
        f"no send landed at {SECOND_HANDLE} within {REPLY_TIMEOUT:.0f}s"
    )


def _prompt_human(instruction: str) -> None:
    print(f"\n>>> NOW: {instruction} (waiting up to {REPLY_TIMEOUT:.0f}s)...",
          flush=True)


@WATCH_TIMEOUT
@E2E
@NEEDS_HANDLE
@NEEDS_SECOND
async def test_watch_set_in_thread_fires_send_and_reports() -> None:
    """#171 AC 1: watch set conversationally → trigger from the second handle →
    the real engine judges relevance → a real send lands there → fire-report in
    the self-thread → the watch retires."""
    runner = _runner()
    head = await _store_head(runner)
    token = f"KIWI-{uuid.uuid4().hex[:6].upper()}"

    await _send_as_phone(
        runner,
        f"Set a watch on {SECOND_HANDLE} for their next message about the test "
        f"package. When it fires, reply to them with exactly the words "
        f"'delivery confirmed {token}' and report back to me here. "
        f"One hour is plenty.",
    )
    await _await_watch_state(runner, "armed")

    _prompt_human(
        f"text from {SECOND_HANDLE}: 'the test package just arrived!'"
    )
    # A real send addressed to the second handle lands in the store...
    await _await_outbound_to_second(runner, head)
    # ...and the report-tone fire-report proves the exact reply chief sent AND
    # names the recipient — the content check the null-text outbound row can't give.
    report = await _await_bot_echo(runner, head, re.escape(token))
    assert any(SECOND_HANDLE in str(row["text"]) for row in report), (
        "the fire-report names the recipient handle"
    )

    fired = await _await_watch_state(runner, "fired")
    assert fired["tone"] == "report"


@WATCH_TIMEOUT
@E2E
@NEEDS_HANDLE
@NEEDS_SECOND
async def test_silent_watch_ignores_irrelevant_and_fires_quiet() -> None:
    """#171 AC 2: an irrelevant trigger produces no send; the relevant one fires
    a real send with **no** self-thread report (silent tone)."""
    runner = _runner()
    head = await _store_head(runner)
    token = f"LIME-{uuid.uuid4().hex[:6].upper()}"

    await _send_as_phone(
        runner,
        f"Set a watch on {SECOND_HANDLE} for their next message about the blue "
        f"umbrella. When it fires, reply to them with exactly the words "
        f"'umbrella ready {token}'. Execute silently — no report back to me. "
        f"One hour is plenty.",
    )
    await _await_watch_state(runner, "armed", tone="silent")

    _prompt_human(f"text from {SECOND_HANDLE}: 'what should we eat tonight?'")
    trigger_head = await _store_head(runner)
    await asyncio.sleep(LOOP_GRACE * 3)
    assert await _outbound_to_second(runner, head) == [], (
        "an irrelevant trigger produced a send"
    )

    _prompt_human(f"text from {SECOND_HANDLE}: 'is the blue umbrella ready?'")
    # A real send lands at the handle (content unverifiable here: silent tone
    # posts no report, and the self-aliased outbound row's text is NULL).
    await _await_outbound_to_second(runner, trigger_head)
    await asyncio.sleep(LOOP_GRACE)
    # Silent tone: no "✅ Replied …" fire-report in the self-thread.
    assert await _bot_echo_rows(runner, trigger_head, r"Replied to") == [], (
        "a silent-tone fire posted a self-thread report"
    )
    fired = await _await_watch_state(runner, "fired", tone="silent")
    assert fired["state"] == "fired"


@WATCH_TIMEOUT
@E2E
@NEEDS_HANDLE
@NEEDS_SECOND
async def test_nonwatched_send_attempt_is_refused_live() -> None:
    """#171 AC 3: with no armed watch, asking chief to text the second handle is
    refused at the send seam and surfaced; nothing lands at the handle."""
    runner = _runner()
    assert not [
        row for row in await _watch_states(runner) if row["state"] == "armed"
    ], "precondition: no armed watch may exist on the second handle"
    head = await _store_head(runner)
    token = f"PLUM-{uuid.uuid4().hex[:6].upper()}"

    await _send_as_phone(
        runner,
        f"Text {SECOND_HANDLE} right now saying 'hi from chief {token}'.",
    )

    await _await_bot_echo(runner, head, r".")  # some reply surfaced the outcome
    await asyncio.sleep(LOOP_GRACE)
    assert await _outbound_to_second(runner, head) == [], (
        "a non-watched send reached the second handle"
    )
