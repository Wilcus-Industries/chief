"""Permissions-doctor + family-gating tests (#155), on the fake-runner seam.

The doctor's probes run through the same subprocess boundary as the tools; the fake
replays realistic captured outputs — including the TCC denial shapes — and the tests
assert the health data (per-capability status, grant label, System Settings
walk-through) plus the per-capability registration decisions the family makes from
it.
"""

from typing import Any

from chief.tools.apple.doctor import (
    CAPABILITIES,
    HEALTH_TOOL,
    MESSAGES_PROBE_QUERY,
    STATUS_DENIED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    AppleDoctorService,
    CapabilityHealth,
    probe_all,
    render_checklist,
)
from chief.tools.apple.family import AppleToolFamily
from chief.tools.apple.runner import (
    NOT_FOUND_EXIT_CODE,
    OSASCRIPT_PATH,
    PBPASTE_PATH,
    SHORTCUTS_PATH,
    SQLITE3_PATH,
    ScriptResult,
    ScriptRunner,
)

OK = ScriptResult("3\n", "", 0)
AUTOMATION_DENIED = ScriptResult(
    "",
    "execution error: Not authorized to send Apple events to Contacts. (-1743)",
    1,
)
FULL_DISK_DENIED = ScriptResult(
    "",
    'Error: unable to open database "/Users/w/Library/Messages/chat.db": '
    "unable to open database file",
    1,
)


class RoutingFakeRunner(ScriptRunner):
    """Routes each probe to a canned result by the binary it drives."""

    def __init__(self, results: dict[str, ScriptResult]) -> None:
        super().__init__()
        self.calls: list[tuple[str, ...]] = []
        self._results = results

    async def run(
        self, argv: Any, *, stdin: bytes | None = None
    ) -> ScriptResult:
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[0] == OSASCRIPT_PATH:
            # Key JXA probes by the app the fixed script drives.
            script = argv[4]
            for app in ("Reminders", "Notes", "Contacts", "Calendar"):
                if app in script:
                    return self._results.get(app.lower(), OK)
            return OK
        if argv[0] == SHORTCUTS_PATH:
            return self._results.get("shortcuts", OK)
        if argv[0] == SQLITE3_PATH:
            return self._results.get("messages", OK)
        if argv[0] == PBPASTE_PATH:
            return self._results.get("system", OK)
        raise AssertionError(f"unexpected probe argv: {argv!r}")


async def test_probe_all_reports_every_capability_in_canonical_order() -> None:
    runner = RoutingFakeRunner({})
    health = await probe_all(runner, messages_db_path="/db")

    assert tuple(h.capability for h in health) == CAPABILITIES
    assert all(h.status == STATUS_OK for h in health)
    # The Messages probe hit the right store with the cheap count query.
    assert (SQLITE3_PATH, "-readonly", "-json", "/db", MESSAGES_PROBE_QUERY) in (
        runner.calls
    )


async def test_probe_all_maps_denials_to_grant_and_walkthrough() -> None:
    runner = RoutingFakeRunner(
        {"contacts": AUTOMATION_DENIED, "messages": FULL_DISK_DENIED}
    )
    health = {h.capability: h for h in await probe_all(runner,
                                                       messages_db_path="/db")}

    contacts = health["contacts"]
    assert contacts.status == STATUS_DENIED
    assert contacts.grant == "Automation → Contacts"
    assert "System Settings → Privacy & Security → Automation" in contacts.fix
    assert "Contacts" in contacts.fix

    msgs = health["messages"]
    assert msgs.status == STATUS_DENIED
    assert msgs.grant == "Full Disk Access"
    assert "Full Disk Access" in msgs.fix
    assert "chat.db" in msgs.fix


async def test_probe_all_flags_missing_binary_and_plain_errors() -> None:
    runner = RoutingFakeRunner(
        {
            "shortcuts": ScriptResult(
                "", "/usr/bin/shortcuts: not found or not runnable",
                NOT_FOUND_EXIT_CODE,
            ),
            "calendar": ScriptResult("", "execution error: some AppleScript bug", 1),
        }
    )
    health = {h.capability: h for h in await probe_all(runner,
                                                       messages_db_path="/db")}

    assert health["shortcuts"].status == STATUS_UNAVAILABLE
    assert health["calendar"].status == STATUS_ERROR
    assert "some AppleScript bug" in health["calendar"].detail


def test_health_rows_are_web_ui_consumable_data() -> None:
    # #153: the web health page renders the doctor's output as data, not prose.
    row = CapabilityHealth(
        capability="notes",
        grant="Automation → Notes",
        status=STATUS_DENIED,
        detail="execution error: Not authorized ... (-1743)",
        fix="Open System Settings ...",
    )
    assert row.as_dict() == {
        "capability": "notes",
        "grant": "Automation → Notes",
        "status": STATUS_DENIED,
        "detail": "execution error: Not authorized ... (-1743)",
        "fix": "Open System Settings ...",
    }


def test_render_checklist_shows_status_fix_and_restart_note() -> None:
    items = [
        CapabilityHealth("reminders", "Automation → Reminders", STATUS_OK,
                         "granted", ""),
        CapabilityHealth(
            "messages", "Full Disk Access", STATUS_DENIED,
            "unable to open database", "Open System Settings → ... Full Disk Access",
        ),
    ]
    text = render_checklist(items)
    assert "[PASS] reminders" in text
    assert "[DENIED] messages" in text
    assert "fix: Open System Settings" in text
    assert "restart" in text  # grants apply at the next boot


def test_render_checklist_all_green_omits_the_restart_note() -> None:
    items = [
        CapabilityHealth("reminders", "Automation → Reminders", STATUS_OK,
                         "granted", ""),
    ]
    assert "restart" not in render_checklist(items)


async def test_doctor_tool_probes_on_demand_and_renders_the_checklist() -> None:
    runner = RoutingFakeRunner({"contacts": AUTOMATION_DENIED})
    service = AppleDoctorService(runner=runner, messages_db_path="/db")
    config = service.server_config()
    handler = next(t for t in config["tools"] if t.name == "check_apple_health")

    result = await handler.handler({})

    text = result["content"][0]["text"]
    assert "[DENIED] contacts" in text
    assert "[PASS] reminders" in text
    assert HEALTH_TOOL == "mcp__chief_apple_doctor__check_apple_health"


# ---- family gating: per-capability registration ----------------------------------


def _family(runner: ScriptRunner | None = None) -> AppleToolFamily:
    return AppleToolFamily(
        runner=runner or ScriptRunner(),
        messages_db_path="/db",
        screenshots_dir="/shots",
    )


def _all_ok() -> list[CapabilityHealth]:
    return [
        CapabilityHealth(cap, "grant", STATUS_OK, "granted", "")
        for cap in CAPABILITIES
    ]


def test_family_registers_every_capability_when_all_probes_pass() -> None:
    services = _family().build_services(_all_ok())
    # messages_send is probe-only (#156): it gates the iMessage adapter's send
    # path, so no tool service registers off it even when green.
    expected = tuple(c for c in CAPABILITIES if c != "messages_send")
    assert tuple(s.capability for s in services) == expected + ("doctor",)
    assert {s.server_name for s in services} == {
        "chief_apple_reminders",
        "chief_apple_notes",
        "chief_apple_contacts",
        "chief_apple_calendar",
        "chief_apple_shortcuts",
        "chief_apple_messages",
        "chief_apple_system",
        "chief_apple_doctor",
    }


def test_family_degrades_per_capability_not_wholesale() -> None:
    health = [
        h
        if h.capability not in ("contacts", "messages")
        else CapabilityHealth(h.capability, h.grant, STATUS_DENIED, "denied", "fix")
        for h in _all_ok()
    ]
    services = _family().build_services(health)
    capabilities = {s.capability for s in services}
    assert "contacts" not in capabilities
    assert "messages" not in capabilities
    # The rest of the family survives, and the doctor always registers.
    assert {"reminders", "notes", "calendar", "shortcuts", "system",
            "doctor"} <= capabilities


def test_family_with_everything_denied_still_registers_the_doctor() -> None:
    health = [
        CapabilityHealth(cap, "grant", STATUS_DENIED, "denied", "fix")
        for cap in CAPABILITIES
    ]
    services = _family().build_services(health)
    assert [s.capability for s in services] == ["doctor"]


async def test_family_check_health_is_the_probe_hook(tmp_path: Any) -> None:
    # The web UI health page (#153) consumes this exact hook.
    runner = RoutingFakeRunner({"notes": AUTOMATION_DENIED})
    health = await _family(runner).check_health()
    by_cap = {h.capability: h for h in health}
    assert by_cap["notes"].status == STATUS_DENIED
    assert by_cap["reminders"].status == STATUS_OK
