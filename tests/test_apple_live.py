"""Opt-in live tests (#155): the Apple family against the real macOS automation
layer.

Skipped unless ``CHIEF_APPLE_LIVE`` is set AND the platform is darwin — the same
pattern as the live Copilot suite (``tests/test_copilot_backend_live.py``). This is
where the PRD's central mechanism runs for real: fixed JXA via ``osascript``, the
Shortcuts CLI, and the sqlite3 CLI against the live Messages store — create, read
back, clean up — plus the permissions doctor probing genuine TCC state. It doubles
as the canary for Apple changing TCC behavior or scripting dictionaries across macOS
releases (supported floor: macOS 14 — see DESIGN.md), and it **re-validates the
captured output shapes** the CI fake-runner suites replay, so the fakes can't drift.

Run it on the Mac (the test rig), with every capability green on the doctor first::

    CHIEF_APPLE_LIVE=1 uv run pytest tests/test_apple_live.py

Prereqs: the terminal app running the tests holds the Automation grants for
Reminders/Notes/Contacts/Calendar and Full Disk Access (for the Messages store).
Each mutating test creates its own uniquely-named item and deletes it afterwards
through a test-local JXA cleanup (the product tools deliberately ship no delete).
"""

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from chief.tools.apple import calendar as apple_calendar
from chief.tools.apple import contacts as apple_contacts
from chief.tools.apple import messages as apple_messages
from chief.tools.apple import notes as apple_notes
from chief.tools.apple import reminders as apple_reminders
from chief.tools.apple.doctor import CAPABILITIES, probe_all, render_checklist
from chief.tools.apple.runner import ScriptRunner
from chief.tools.inprocess import InProcessServerConfig

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_APPLE_LIVE") or sys.platform != "darwin",
    reason="live Apple test — set CHIEF_APPLE_LIVE=1 on a Mac with the TCC grants",
)

#: Real TCC prompts / first app launches can take a while; override the 30s cap.
LIVE_TIMEOUT = pytest.mark.timeout(120)

#: The default Messages store on the machine under test.
CHAT_DB = str(Path.home() / "Library/Messages/chat.db")


def _runner() -> ScriptRunner:
    return ScriptRunner(timeout=60.0)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# Test-local cleanup scripts (argv: [exact name]). The product ships no delete tools
# by design; the live suite still must not leave droppings on the owner's Mac.
_DELETE_REMINDER = (
    "function run(argv) {\n"
    "  const app = Application('Reminders');\n"
    "  for (const list of app.lists()) {\n"
    "    const hits = list.reminders.whose({name: argv[0]})();\n"
    "    if (hits.length > 0) { app.delete(hits[0]); return 'deleted'; }\n"
    "  }\n"
    "  return 'not found';\n"
    "}"
)
_DELETE_NOTE = (
    "function run(argv) {\n"
    "  const app = Application('Notes');\n"
    "  const hits = app.notes.whose({name: argv[0]})();\n"
    "  if (hits.length > 0) { app.delete(hits[0]); return 'deleted'; }\n"
    "  return 'not found';\n"
    "}"
)
_DELETE_CONTACT = (
    "function run(argv) {\n"
    "  const app = Application('Contacts');\n"
    "  const hits = app.people.whose({name: argv[0]})();\n"
    "  if (hits.length > 0) { app.delete(hits[0]); app.save(); return 'deleted'; }\n"
    "  return 'not found';\n"
    "}"
)
_CREATE_CONTACT = (
    "function run(argv) {\n"
    "  const app = Application('Contacts');\n"
    "  const person = app.Person({firstName: argv[0], lastName: argv[1]});\n"
    "  app.people.push(person);\n"
    "  person.phones.push(app.Phone({label: 'mobile', value: argv[2]}));\n"
    "  app.save();\n"
    "  return 'created';\n"
    "}"
)
_DELETE_EVENT = (
    "function run(argv) {\n"
    "  const app = Application('Calendar');\n"
    "  for (const cal of app.calendars()) {\n"
    "    const hits = cal.events.whose({summary: argv[0]})();\n"
    "    if (hits.length > 0) { app.delete(hits[0]); return 'deleted'; }\n"
    "  }\n"
    "  return 'not found';\n"
    "}"
)


async def _call(
    service_config: InProcessServerConfig, name: str, args: dict[str, Any]
) -> str:
    handler = next(t for t in service_config["tools"] if t.name == name).handler
    result = await handler(args)
    assert result["is_error"] is False, result
    return str(result["content"][0]["text"])


# ---- doctor against genuine TCC state ------------------------------------------


@LIVE_TIMEOUT
async def test_doctor_probes_genuine_tcc_state() -> None:
    health = await probe_all(_runner(), messages_db_path=CHAT_DB)
    assert tuple(h.capability for h in health) == CAPABILITIES
    # Print the real checklist so a failing rig run shows exactly what to grant.
    print(render_checklist(health))
    # The acceptance demo wants everything green before the story list runs.
    not_ok = [h for h in health if not h.ok]
    assert not not_ok, f"grants missing: {[h.capability for h in not_ok]}"


# ---- per-capability roundtrips (create → read back → clean up) -------------------


@LIVE_TIMEOUT
async def test_reminders_roundtrip() -> None:
    runner = _runner()
    service = apple_reminders.RemindersService(runner=runner).server_config()
    name = _unique("chief-live-reminder")
    try:
        await _call(service, "create_reminder", {"name": name})
        listing = await _call(service, "list_reminders", {})
        assert name in listing
        # Re-validate the fake fixtures' captured JSON shape (keys) against the
        # real scripting dictionary so the CI fakes can't drift.
        raw = await runner.run_jxa(apple_reminders.LIST_SCRIPT, [""])
        rows = json.loads(raw.stdout)
        match = next(r for r in rows if r["name"] == name)
        assert set(match) == {"name", "body", "list", "due"}
        completed = await _call(service, "complete_reminder", {"name": name})
        assert "Completed" in completed
    finally:
        await runner.run_jxa(_DELETE_REMINDER, [name])


@LIVE_TIMEOUT
async def test_notes_roundtrip() -> None:
    runner = _runner()
    service = apple_notes.NotesService(runner=runner).server_config()
    title = _unique("chief-live-note")
    try:
        await _call(
            service, "create_note", {"title": title, "body": "boiler pilot relit"}
        )
        hits = await _call(service, "search_notes", {"query": title})
        assert title in hits
        raw = await runner.run_jxa(apple_notes.SEARCH_SCRIPT, [title])
        rows = json.loads(raw.stdout)
        assert rows and set(rows[0]) == {"name", "snippet", "modified"}
        body = await _call(service, "read_note", {"title": title})
        assert "boiler pilot relit" in body
    finally:
        await runner.run_jxa(_DELETE_NOTE, [title])


@LIVE_TIMEOUT
async def test_contacts_lookup_roundtrip() -> None:
    runner = _runner()
    service = apple_contacts.ContactsService(runner=runner).server_config()
    first, last = "ChiefLive", _unique("Test")
    full = f"{first} {last}"
    created = await runner.run_jxa(_CREATE_CONTACT, [first, last, "+15550100999"])
    assert created.ok, created
    try:
        text = await _call(service, "lookup_contact", {"name": last})
        assert full in text
        assert "+15550100999" in text
        raw = await runner.run_jxa(apple_contacts.LOOKUP_SCRIPT, [last])
        rows = json.loads(raw.stdout)
        assert rows and set(rows[0]) == {"name", "phones", "emails"}
        assert set(rows[0]["phones"][0]) == {"label", "number"}
    finally:
        await runner.run_jxa(_DELETE_CONTACT, [full])


@LIVE_TIMEOUT
async def test_apple_calendar_roundtrip() -> None:
    runner = _runner()
    service = apple_calendar.AppleCalendarService(runner=runner).server_config()
    summary = _unique("chief-live-event")
    try:
        await _call(
            service,
            "create_event",
            {
                "summary": summary,
                "start": "2027-01-15T12:00:00",
                "end": "2027-01-15T13:00:00",
            },
        )
        agenda = await _call(
            service,
            "list_events",
            {"start": "2027-01-15T00:00:00", "end": "2027-01-16T00:00:00"},
        )
        assert summary in agenda
        raw = await runner.run_jxa(
            apple_calendar.LIST_SCRIPT,
            ["2027-01-15T00:00:00", "2027-01-16T00:00:00", ""],
        )
        rows = json.loads(raw.stdout)
        match = next(r for r in rows if r["summary"] == summary)
        assert set(match) == {"summary", "start", "end", "calendar", "location"}
    finally:
        await runner.run_jxa(_DELETE_EVENT, [summary])


@LIVE_TIMEOUT
async def test_shortcuts_list_runs_for_real() -> None:
    # List-only: running a shortcut mutates whatever the owner automated, so the
    # live default stays read-only (run_shortcut is exercised via the acceptance
    # demo with a purpose-built no-op shortcut instead).
    result = await _runner().run_shortcuts(["list"])
    assert result.ok, result
    # Line-per-name output — the exact shape the CI fake replays.
    assert result.stdout == "" or all(
        line.strip() for line in result.stdout.strip().splitlines()
    )


@LIVE_TIMEOUT
async def test_system_clipboard_roundtrip_restores_previous_content(
    tmp_path: Path,
) -> None:
    from chief.tools.apple.system import SystemService

    runner = _runner()
    service = SystemService(
        runner=runner, screenshots_dir=str(tmp_path)
    ).server_config()
    saved = await runner.run([runner.pbpaste_path])
    marker = _unique("chief-live-clip")
    try:
        await _call(service, "clipboard_write", {"text": marker})
        read_back = await _call(service, "clipboard_read", {})
        assert read_back == marker
    finally:
        await runner.run([runner.pbcopy_path], stdin=saved.stdout.encode())


@LIVE_TIMEOUT
async def test_system_notify_and_screenshot(tmp_path: Path) -> None:
    from chief.tools.apple.system import SystemService

    runner = _runner()
    service = SystemService(
        runner=runner, screenshots_dir=str(tmp_path)
    ).server_config()
    await _call(
        service, "notify", {"message": "live suite says hi", "title": "chief"}
    )
    text = await _call(service, "screenshot", {})
    path = Path(text.removeprefix("Screenshot saved to ").removesuffix("."))
    assert path.is_file() and path.stat().st_size > 0
    path.unlink()


@LIVE_TIMEOUT
async def test_messages_history_reads_the_real_store() -> None:
    runner = _runner()
    service = apple_messages.MessagesService(
        runner=runner, db_path=CHAT_DB
    ).server_config()
    # Read-only smoke: the store opens (Full Disk Access) and rows parse. An empty
    # store is a pass — the query itself succeeding is what exercises the grant.
    text = await _call(service, "recent_messages", {"limit": 5})
    assert text  # either formatted rows or the no-matches note
    raw = await runner.run_sqlite(
        CHAT_DB, apple_messages.build_recent_query("", 1)
    )
    assert raw.ok, raw
    if raw.stdout.strip():
        rows = json.loads(raw.stdout)
        assert set(rows[0]) == {"timestamp", "sender", "is_from_me", "text"}
