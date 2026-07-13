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
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from chief.adapters.imessage import (
    IMessageTaskIO,
    build_head_query,
    build_poll_query,
)
from chief.tools.apple.doctor import probe_all
from chief.tools.apple.runner import ScriptRunner
from imessage_helpers import CHAT_DB_SCHEMA

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
    tables = {"handle", "chat", "message", "chat_message_join"}
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
    """A real send, asserted by reading the sent row back out of the store."""
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
        rows = json.loads(result.stdout or "[]")
        if rows:
            assert rows[0]["is_from_me"] == 1
            return
        await asyncio.sleep(1)
    raise AssertionError("sent message never appeared in the live store")
