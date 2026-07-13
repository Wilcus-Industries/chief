"""The Apple permissions doctor (#155): probe TCC grants, walk the owner through
fixes.

macOS gates each Apple capability behind a TCC grant (Automation per app, Full Disk
Access for the Messages store). The doctor probes each grant's real state through the
same :class:`~chief.tools.apple.runner.ScriptRunner` seam the tools use, and reports
a per-capability health checklist **as data** (:class:`CapabilityHealth`, JSON-able
via :meth:`CapabilityHealth.as_dict`) so both chat (``render_checklist``) and the web
UI health page (#153 — consume :func:`probe_all` / ``AppleToolFamily.check_health``)
can present it. Each non-ok capability carries the exact System Settings walk-through
for its missing grant.

Capabilities degrade individually: boot registration keys off each capability's own
probe (:mod:`chief.tools.apple.family`), so a missing Contacts grant disables contact
lookup, not the family. The doctor itself always registers on a Mac — it is how the
owner discovers what to fix. Grants flip in System Settings while chief runs, so the
checklist notes that a restart registers newly-granted capabilities.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .runner import (
    NOT_FOUND_EXIT_CODE,
    ScriptResult,
    ScriptRunner,
    is_permission_denied,
    text_result,
)

SERVER_NAME = "chief_apple_doctor"
HEALTH_TOOL = f"mcp__{SERVER_NAME}__check_apple_health"

STATUS_OK = "ok"
STATUS_DENIED = "denied"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"

#: The canonical capability order (the checklist and registration both follow it).
CAPABILITIES: tuple[str, ...] = (
    "reminders",
    "notes",
    "contacts",
    "calendar",
    "shortcuts",
    "messages",
    "system",
)

#: Cheap JXA probes: one count Apple event per app — enough to trip (or verify) the
#: Automation TCC grant without touching owner data.
_JXA_PROBES: dict[str, str] = {
    "reminders": (
        "function run() { return String(Application('Reminders').lists.length); }"
    ),
    "notes": (
        "function run() { return String(Application('Notes').folders.length); }"
    ),
    "contacts": (
        "function run() { return String(Application('Contacts').people.length); }"
    ),
    "calendar": (
        "function run() { return String(Application('Calendar').calendars.length); }"
    ),
}

#: The Messages-store probe: the cheapest read that still exercises Full Disk Access.
MESSAGES_PROBE_QUERY = "SELECT count(*) AS n FROM message LIMIT 1;"

#: The TCC grant behind each capability, as the owner sees it in System Settings.
_GRANTS: dict[str, str] = {
    "reminders": "Automation → Reminders",
    "notes": "Automation → Notes",
    "contacts": "Automation → Contacts",
    "calendar": "Automation → Calendar",
    "shortcuts": "Automation → Shortcuts",
    "messages": "Full Disk Access",
    "system": "None (clipboard); Screen Recording affects screenshots",
}

_AUTOMATION_FIX = (
    "Open System Settings → Privacy & Security → Automation, find the app chief "
    "runs under (e.g. Terminal), and enable {app}. If it is not listed yet, run any "
    "{app} tool once so macOS shows the consent prompt, and click Allow. Restart "
    "chief afterwards so the capability registers."
)
_FULL_DISK_FIX = (
    "Open System Settings → Privacy & Security → Full Disk Access, add (or enable) "
    "the app chief runs under (e.g. Terminal), then restart chief. The Messages "
    "history lives in ~/Library/Messages/chat.db, which only Full Disk Access "
    "unlocks."
)
_SYSTEM_FIX = (
    "The clipboard needs no grant — this failure is unexpected; check that pbpaste "
    "works in a terminal. If screenshots capture only the wallpaper, open System "
    "Settings → Privacy & Security → Screen & System Audio Recording and enable the "
    "app chief runs under, then restart chief."
)

#: capability → the exact System Settings walk-through for its grant.
_FIXES: dict[str, str] = {
    "reminders": _AUTOMATION_FIX.format(app="Reminders"),
    "notes": _AUTOMATION_FIX.format(app="Notes"),
    "contacts": _AUTOMATION_FIX.format(app="Contacts"),
    "calendar": _AUTOMATION_FIX.format(app="Calendar"),
    "shortcuts": _AUTOMATION_FIX.format(app="Shortcuts"),
    "messages": _FULL_DISK_FIX,
    "system": _SYSTEM_FIX,
}

#: Caveats that hold even when a probe passes.
_OK_NOTES: dict[str, str] = {
    "system": (
        "Screen Recording cannot be probed without capturing; if screenshots show "
        "only the wallpaper, grant it (see the fix steps)."
    ),
}


@dataclass(frozen=True)
class CapabilityHealth:
    """One capability's probed health — data, not prose (web UI + chat share it)."""

    capability: str
    grant: str
    status: str
    detail: str
    fix: str

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, str]:
        """A JSON-able row for the web UI health page (#153)."""
        return {
            "capability": self.capability,
            "grant": self.grant,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


def _classify(capability: str, result: ScriptResult) -> CapabilityHealth:
    """Map one probe's :class:`ScriptResult` onto a :class:`CapabilityHealth`."""
    grant = _GRANTS[capability]
    fix = _FIXES[capability]
    if result.ok:
        return CapabilityHealth(
            capability, grant, STATUS_OK, _OK_NOTES.get(capability, "granted"), ""
        )
    if is_permission_denied(result):
        detail = result.stderr.strip() or result.stdout.strip()
        return CapabilityHealth(capability, grant, STATUS_DENIED, detail, fix)
    if result.exit_code == NOT_FOUND_EXIT_CODE:
        return CapabilityHealth(
            capability,
            grant,
            STATUS_UNAVAILABLE,
            result.stderr.strip(),
            "This macOS install is missing the binary the capability drives — "
            "check the macOS version (see the supported floor in DESIGN.md).",
        )
    detail = result.stderr.strip() or result.stdout.strip() or (
        f"probe failed with exit code {result.exit_code}"
    )
    return CapabilityHealth(capability, grant, STATUS_ERROR, detail, fix)


async def probe_all(
    runner: ScriptRunner, *, messages_db_path: str
) -> list[CapabilityHealth]:
    """Probe every capability's grant state, concurrently; canonical order.

    This is the load-bearing data source: boot registration
    (:meth:`~chief.tools.apple.family.AppleToolFamily.build_services`), the chat
    doctor tool, and the web UI health page (#153) all consume its output.
    """
    probes = {
        capability: runner.run_jxa(script)
        for capability, script in _JXA_PROBES.items()
    }
    probes["shortcuts"] = runner.run_shortcuts(["list"])
    probes["messages"] = runner.run_sqlite(messages_db_path, MESSAGES_PROBE_QUERY)
    probes["system"] = runner.run([runner.pbpaste_path])
    results = await asyncio.gather(*probes.values())
    by_capability = dict(zip(probes.keys(), results, strict=True))
    return [_classify(cap, by_capability[cap]) for cap in CAPABILITIES]


def render_checklist(items: list[CapabilityHealth]) -> str:
    """The chat rendering of the health checklist (the web UI renders the data)."""
    lines = ["Apple capability health:"]
    for item in items:
        mark = "PASS" if item.ok else item.status.upper()
        lines.append(f"[{mark}] {item.capability} — {item.grant}: {item.detail}")
        if item.fix:
            lines.append(f"       fix: {item.fix}")
    if any(not item.ok for item in items):
        lines.append(
            "Capabilities register at boot — after granting a permission, restart "
            "chief to pick it up."
        )
    return "\n".join(lines)


_HEALTH_DESCRIPTION = (
    "Check the health of every Apple capability (Reminders, Notes, Contacts, "
    "Calendar, Shortcuts, Messages history, system control) by probing the macOS "
    "permission behind each. Reports a per-capability checklist with the exact "
    "System Settings steps for anything missing. Run this when an Apple tool fails "
    "or is absent."
)

_HEALTH_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


@dataclass(frozen=True)
class AppleDoctorService:
    """Builds the owner session's permissions-doctor server (always registered on a
    Mac, even when every other capability is denied — it is the way out)."""

    runner: ScriptRunner
    messages_db_path: str
    server_name: str = SERVER_NAME
    capability: str = "doctor"

    def _build_health(self) -> InProcessTool:
        runner, db_path = self.runner, self.messages_db_path

        @tool("check_apple_health", _HEALTH_DESCRIPTION, _HEALTH_SCHEMA)
        async def check_apple_health(args: dict[str, Any]) -> dict[str, Any]:
            health = await probe_all(runner, messages_db_path=db_path)
            return text_result(render_checklist(health))

        return check_apple_health

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the doctor tool."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_health()])
