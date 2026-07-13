"""Apple per-app service tests (#155): the fake-runner CI suite.

Each service is exercised against a fake ScriptRunner sitting at the exact
subprocess boundary (argv + stdin in, ScriptResult out — the PRD's declared test
seam). Tests assert the *exact* generated osascript/Shortcuts-CLI/sqlite3
invocations and the parsing of realistic captured outputs, including the
permission-denied shapes that route the owner to the doctor. The env-gated on-Mac
live suite (tests/test_apple_live.py) runs the same calls for real and re-validates
these captured shapes.
"""

import json
from pathlib import Path
from typing import Any

from chief.tools.apple import (
    calendar,
    contacts,
    messages,
    notes,
    reminders,
    system,
)
from chief.tools.apple import shortcuts as apple_shortcuts
from chief.tools.apple.runner import (
    OSASCRIPT_PATH,
    SHORTCUTS_PATH,
    SQLITE3_PATH,
    ScriptResult,
    ScriptRunner,
)
from chief.tools.inprocess import InProcessServerConfig


class FakeRunner(ScriptRunner):
    """A ScriptRunner faked at the subprocess boundary: records argv/stdin and
    replays queued results (the last one repeats)."""

    def __init__(self, *results: ScriptResult) -> None:
        super().__init__()
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self._results = list(results) or [ScriptResult("", "", 0)]

    async def run(
        self, argv: Any, *, stdin: bytes | None = None
    ) -> ScriptResult:
        self.calls.append((tuple(argv), stdin))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _handler(config: InProcessServerConfig, name: str) -> Any:
    """The named tool's async handler from a service's server config."""
    return next(t for t in config["tools"] if t.name == name).handler


def _jxa_argv(script: str, *args: str) -> tuple[str, ...]:
    return (OSASCRIPT_PATH, "-l", "JavaScript", "-e", script, *args)


def _text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


#: Captured osascript denial (Automation TCC) — the shape the doctor keys off.
DENIED = ScriptResult(
    "",
    "execution error: Not authorized to send Apple events to Reminders. (-1743)",
    1,
)


# ---- Reminders -----------------------------------------------------------------


async def test_create_reminder_generates_the_exact_jxa_invocation() -> None:
    runner = FakeRunner(ScriptResult("created in Reminders\n", "", 0))
    handler = _handler(
        reminders.RemindersService(runner=runner).server_config(),
        "create_reminder",
    )

    result = await handler(
        {"name": "take the bins out", "due": "2026-07-14T20:00:00"}
    )

    assert runner.calls == [
        (
            _jxa_argv(
                reminders.CREATE_SCRIPT,
                "take the bins out",
                "",
                "2026-07-14T20:00:00",
                "",
            ),
            None,
        )
    ]
    assert result["is_error"] is False
    assert "take the bins out" in _text(result)


async def test_list_reminders_parses_captured_json_output() -> None:
    captured = json.dumps(
        [
            {
                "name": "take the bins out",
                "body": None,
                "list": "Reminders",
                "due": "2026-07-14T18:00:00.000Z",
            },
            {"name": "call mum", "body": "about the cabin", "list": "Family",
             "due": None},
        ]
    )
    runner = FakeRunner(ScriptResult(captured + "\n", "", 0))
    handler = _handler(
        reminders.RemindersService(runner=runner).server_config(), "list_reminders"
    )

    result = await handler({})

    assert runner.calls == [(_jxa_argv(reminders.LIST_SCRIPT, ""), None)]
    text = _text(result)
    assert "take the bins out" in text and "call mum" in text
    assert "due 2026-07-14T18:00:00.000Z" in text


async def test_list_reminders_scopes_to_a_named_list() -> None:
    runner = FakeRunner(ScriptResult("[]", "", 0))
    handler = _handler(
        reminders.RemindersService(runner=runner).server_config(), "list_reminders"
    )

    result = await handler({"list": "Family"})

    assert runner.calls == [(_jxa_argv(reminders.LIST_SCRIPT, "Family"), None)]
    assert _text(result) == "No open reminders."


async def test_complete_reminder_reports_hit_and_miss() -> None:
    runner = FakeRunner(
        ScriptResult("completed\n", "", 0), ScriptResult("not found\n", "", 0)
    )
    handler = _handler(
        reminders.RemindersService(runner=runner).server_config(),
        "complete_reminder",
    )

    hit = await handler({"name": "take the bins out"})
    miss = await handler({"name": "take the bins out"})

    assert runner.calls[0] == (
        _jxa_argv(reminders.COMPLETE_SCRIPT, "take the bins out", ""),
        None,
    )
    assert hit["is_error"] is False
    assert miss["is_error"] is True and "list_reminders" in _text(miss)


async def test_reminders_permission_denial_routes_to_the_doctor() -> None:
    runner = FakeRunner(DENIED)
    handler = _handler(
        reminders.RemindersService(runner=runner).server_config(), "list_reminders"
    )

    result = await handler({})

    assert result["is_error"] is True
    assert "check_apple_health" in _text(result)


# ---- Notes -----------------------------------------------------------------------


async def test_create_note_generates_the_exact_jxa_invocation() -> None:
    runner = FakeRunner(ScriptResult("created\n", "", 0))
    handler = _handler(
        notes.NotesService(runner=runner).server_config(), "create_note"
    )

    result = await handler(
        {"title": "Boiler", "body": "pilot light relit", "folder": "House"}
    )

    assert runner.calls == [
        (
            _jxa_argv(notes.CREATE_SCRIPT, "Boiler", "pilot light relit", "House"),
            None,
        )
    ]
    assert result["is_error"] is False


async def test_search_notes_parses_captured_hits() -> None:
    captured = json.dumps(
        [
            {
                "name": "Boiler service",
                "snippet": "Boiler service\npilot light relit by Gasco",
                "modified": "2026-06-02T10:00:00.000Z",
            }
        ]
    )
    runner = FakeRunner(ScriptResult(captured, "", 0))
    handler = _handler(
        notes.NotesService(runner=runner).server_config(), "search_notes"
    )

    result = await handler({"query": "boiler"})

    assert runner.calls == [(_jxa_argv(notes.SEARCH_SCRIPT, "boiler"), None)]
    text = _text(result)
    assert "Boiler service" in text and "pilot light relit" in text


async def test_read_note_returns_plaintext_and_flags_missing() -> None:
    runner = FakeRunner(
        ScriptResult("Boiler service\npilot light relit\n", "", 0),
        ScriptResult("NOT FOUND\n", "", 0),
    )
    handler = _handler(
        notes.NotesService(runner=runner).server_config(), "read_note"
    )

    found = await handler({"title": "Boiler service"})
    missing = await handler({"title": "Boiler service"})

    assert runner.calls[0] == (
        _jxa_argv(notes.READ_SCRIPT, "Boiler service"),
        None,
    )
    assert "pilot light relit" in _text(found)
    assert missing["is_error"] is True and "search_notes" in _text(missing)


# ---- Contacts ---------------------------------------------------------------------


async def test_lookup_contact_generates_invocation_and_parses_captured_output(
) -> None:
    captured = json.dumps(
        [
            {
                "name": "Mum",
                "phones": [{"label": "mobile", "number": "+15551234567"}],
                "emails": [{"label": "home", "address": "mum@example.com"}],
            }
        ]
    )
    runner = FakeRunner(ScriptResult(captured, "", 0))
    handler = _handler(
        contacts.ContactsService(runner=runner).server_config(), "lookup_contact"
    )

    result = await handler({"name": "Mum"})

    assert runner.calls == [(_jxa_argv(contacts.LOOKUP_SCRIPT, "Mum"), None)]
    text = _text(result)
    assert "mobile: +15551234567" in text
    assert "home: mum@example.com" in text


async def test_lookup_contact_reports_no_match() -> None:
    runner = FakeRunner(ScriptResult("[]", "", 0))
    handler = _handler(
        contacts.ContactsService(runner=runner).server_config(), "lookup_contact"
    )
    assert _text(await handler({"name": "Nobody"})) == "No contacts matched."


# ---- Apple Calendar ----------------------------------------------------------------


async def test_create_event_generates_the_exact_jxa_invocation() -> None:
    runner = FakeRunner(ScriptResult("created on Home\n", "", 0))
    handler = _handler(
        calendar.AppleCalendarService(runner=runner).server_config(), "create_event"
    )

    result = await handler(
        {
            "summary": "Lunch with Sam",
            "start": "2026-07-17T12:00:00",
            "end": "2026-07-17T13:00:00",
            "calendar": "Home",
        }
    )

    assert runner.calls == [
        (
            _jxa_argv(
                calendar.CREATE_SCRIPT,
                "Lunch with Sam",
                "2026-07-17T12:00:00",
                "2026-07-17T13:00:00",
                "Home",
                "",
                "",
            ),
            None,
        )
    ]
    assert "Lunch with Sam" in _text(result)


async def test_list_events_parses_captured_agenda() -> None:
    captured = json.dumps(
        [
            {
                "summary": "Lunch with Sam",
                "start": "2026-07-17T12:00:00.000Z",
                "end": "2026-07-17T13:00:00.000Z",
                "calendar": "Home",
                "location": "Taqueria",
            }
        ]
    )
    runner = FakeRunner(ScriptResult(captured, "", 0))
    handler = _handler(
        calendar.AppleCalendarService(runner=runner).server_config(), "list_events"
    )

    result = await handler(
        {"start": "2026-07-17T00:00:00", "end": "2026-07-18T00:00:00"}
    )

    assert runner.calls == [
        (
            _jxa_argv(
                calendar.LIST_SCRIPT,
                "2026-07-17T00:00:00",
                "2026-07-18T00:00:00",
                "",
            ),
            None,
        )
    ]
    text = _text(result)
    assert "Lunch with Sam" in text and "@ Taqueria" in text


# ---- Shortcuts ----------------------------------------------------------------------


async def test_list_shortcuts_parses_the_cli_line_output() -> None:
    runner = FakeRunner(ScriptResult("Leaving work\nMorning brief\n", "", 0))
    handler = _handler(
        apple_shortcuts.ShortcutsService(runner=runner).server_config(),
        "list_shortcuts",
    )

    result = await handler({})

    assert runner.calls == [((SHORTCUTS_PATH, "list"), None)]
    assert _text(result) == "Leaving work\nMorning brief"


async def test_run_shortcut_without_input_omits_the_stdin_flag() -> None:
    runner = FakeRunner(ScriptResult("done\n", "", 0))
    handler = _handler(
        apple_shortcuts.ShortcutsService(runner=runner).server_config(),
        "run_shortcut",
    )

    result = await handler({"name": "Leaving work"})

    assert runner.calls == [
        ((SHORTCUTS_PATH, "run", "Leaving work", "-o", "-"), None)
    ]
    assert "done" in _text(result)


async def test_run_shortcut_pipes_input_over_stdin() -> None:
    runner = FakeRunner(ScriptResult("", "", 0))
    handler = _handler(
        apple_shortcuts.ShortcutsService(runner=runner).server_config(),
        "run_shortcut",
    )

    result = await handler({"name": "Summarize", "input": "some text"})

    assert runner.calls == [
        (
            (SHORTCUTS_PATH, "run", "Summarize", "-o", "-", "-i", "-"),
            b"some text",
        )
    ]
    assert "no output" in _text(result)


# ---- System control ------------------------------------------------------------------


async def test_clipboard_roundtrip_uses_pb_binaries() -> None:
    runner = FakeRunner(ScriptResult("copied text", "", 0))
    service = system.SystemService(runner=runner, screenshots_dir="/tmp/shots")
    read = _handler(service.server_config(), "clipboard_read")
    write = _handler(service.server_config(), "clipboard_write")

    read_result = await read({})
    await write({"text": "new clipboard"})

    assert runner.calls[0] == ((runner.pbpaste_path,), None)
    assert runner.calls[1] == ((runner.pbcopy_path,), b"new clipboard")
    assert _text(read_result) == "copied text"


async def test_notify_generates_the_exact_jxa_invocation() -> None:
    runner = FakeRunner(ScriptResult("ok\n", "", 0))
    service = system.SystemService(runner=runner, screenshots_dir="/tmp/shots")
    handler = _handler(service.server_config(), "notify")

    result = await handler({"message": "kettle's boiled", "title": "chief"})

    assert runner.calls == [
        (_jxa_argv(system.NOTIFY_SCRIPT, "kettle's boiled", "chief"), None)
    ]
    assert result["is_error"] is False


async def test_screenshot_captures_to_the_screenshots_dir(tmp_path: Path) -> None:
    runner = FakeRunner(ScriptResult("", "", 0))
    service = system.SystemService(
        runner=runner, screenshots_dir=str(tmp_path / "shots")
    )
    handler = _handler(service.server_config(), "screenshot")

    result = await handler({})

    (argv, stdin) = runner.calls[0]
    assert argv[0] == runner.screencapture_path
    assert argv[1] == "-x"  # silent capture
    assert argv[2].startswith(str(tmp_path / "shots")) and argv[2].endswith(".png")
    assert (tmp_path / "shots").is_dir()  # created before the capture
    assert argv[2] in _text(result)
    assert stdin is None


# ---- Messages history ----------------------------------------------------------


async def test_search_messages_builds_the_exact_sqlite_invocation() -> None:
    captured = json.dumps(
        [
            {
                "timestamp": "2026-07-10 09:15:22",
                "sender": "+15551234567",
                "is_from_me": 0,
                "text": "cabin next weekend?",
            },
            {
                "timestamp": "2026-07-10 09:16:01",
                "sender": "+15551234567",
                "is_from_me": 1,
                "text": "yes! packing Friday",
            },
        ]
    )
    runner = FakeRunner(ScriptResult(captured, "", 0))
    service = messages.MessagesService(runner=runner, db_path="/Users/w/chat.db")
    handler = _handler(service.server_config(), "search_messages")

    result = await handler({"query": "cabin", "limit": 5})

    assert runner.calls == [
        (
            (
                SQLITE3_PATH,
                "-readonly",
                "-json",
                "/Users/w/chat.db",
                messages.build_search_query("cabin", 5),
            ),
            None,
        )
    ]
    text = _text(result)
    assert "+15551234567 → me: cabin next weekend?" in text
    assert "me → +15551234567: yes! packing Friday" in text


def test_search_query_escapes_quotes_and_like_wildcards() -> None:
    query = messages.build_search_query("mom's 100%_deal", 20)
    assert "mom''s" in query  # SQL quote doubling
    assert r"100\%\_deal" in query  # LIKE wildcards escaped
    assert query.count("LIMIT 20") == 1


def test_recent_query_optionally_filters_by_handle() -> None:
    bare = messages.build_recent_query("", 20)
    filtered = messages.build_recent_query("mum@example.com", 20)
    assert "handle.id LIKE" not in bare
    assert "'%mum@example.com%'" in filtered
    # Read-only SELECTs, newest first, always bounded.
    for q in (bare, filtered):
        assert q.startswith("SELECT")
        assert "ORDER BY message.date DESC" in q


async def test_recent_messages_empty_stdout_means_no_matches() -> None:
    # sqlite3 -json prints nothing at all for an empty result set.
    runner = FakeRunner(ScriptResult("", "", 0))
    service = messages.MessagesService(runner=runner, db_path="/Users/w/chat.db")
    handler = _handler(service.server_config(), "recent_messages")

    result = await handler({})

    assert _text(result) == "No messages matched."
    assert result["is_error"] is False


async def test_messages_full_disk_denial_routes_to_the_doctor() -> None:
    # Captured shape: chat.db without the Full Disk Access grant.
    denied = ScriptResult(
        "",
        'Error: unable to open database "/Users/w/Library/Messages/chat.db": '
        "unable to open database file",
        1,
    )
    runner = FakeRunner(denied)
    service = messages.MessagesService(runner=runner, db_path="/Users/w/chat.db")
    handler = _handler(service.server_config(), "search_messages")

    result = await handler({"query": "cabin"})

    assert result["is_error"] is True
    assert "check_apple_health" in _text(result)


async def test_messages_limit_is_clamped() -> None:
    runner = FakeRunner(ScriptResult("", "", 0))
    service = messages.MessagesService(runner=runner, db_path="/db")
    handler = _handler(service.server_config(), "recent_messages")

    await handler({"limit": 100_000})
    await handler({"limit": "banana"})

    first_query = runner.calls[0][0][4]
    second_query = runner.calls[1][0][4]
    assert f"LIMIT {messages.MAX_LIMIT};" in first_query
    assert f"LIMIT {messages.DEFAULT_LIMIT};" in second_query
