"""Owner-only Apple Calendar tools (#155): create and list events.

An in-process MCP server (``chief_apple_calendar``) driving Calendar.app through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam. This is the *Apple* calendar,
alongside the existing Google Calendar integration — the owner may live on either.
``create_event`` is seeded into ``blacklist_tools`` (:mod:`chief.config`) so it raises
an approval card, matching the Google calendar write posture; ``list_events`` reads
freely. Events carry no attendees in v1, so a create never reaches other people.
"""

import json
from dataclasses import dataclass
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .runner import ScriptRunner, script_error_result, text_result

SERVER_NAME = "chief_apple_calendar"
CREATE_TOOL = f"mcp__{SERVER_NAME}__create_event"
LIST_TOOL = f"mcp__{SERVER_NAME}__list_events"

#: Calendar writes ASK (approval card), matching the Google calendar posture. Seeded
#: into ``blacklist_tools`` by :mod:`chief.config`.
MUTATING_TOOL_NAMES: tuple[str, ...] = (CREATE_TOOL,)

#: argv: [summary, start-ISO, end-ISO, calendar-name, location, description].
#: Empty calendar name = the owner's first calendar (Calendar.app has no scriptable
#: default); naming the calendar is encouraged in the tool description.
CREATE_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Calendar');\n"
    "  const cal = argv[3] ? app.calendars.byName(argv[3])\n"
    "                      : app.calendars()[0];\n"
    "  const props = {summary: argv[0], startDate: new Date(argv[1]),\n"
    "                 endDate: new Date(argv[2])};\n"
    "  if (argv[4]) props.location = argv[4];\n"
    "  if (argv[5]) props.description = argv[5];\n"
    "  cal.events.push(app.Event(props));\n"
    "  return 'created on ' + cal.name();\n"
    "}"
)

#: argv: [from-ISO, to-ISO, calendar-name] (empty = every calendar). Returns the
#: window's events as JSON, sorted by start.
LIST_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Calendar');\n"
    "  const from = new Date(argv[0]);\n"
    "  const to = new Date(argv[1]);\n"
    "  const cals = argv[2] ? [app.calendars.byName(argv[2])] : app.calendars();\n"
    "  const out = [];\n"
    "  for (const cal of cals) {\n"
    "    const evs = cal.events.whose({startDate: {_greaterThanEquals: from},\n"
    "                                  endDate: {_lessThanEquals: to}})();\n"
    "    for (const ev of evs) {\n"
    "      out.push({summary: ev.summary(), start: ev.startDate().toISOString(),\n"
    "                end: ev.endDate().toISOString(), calendar: cal.name(),\n"
    "                location: ev.location()});\n"
    "    }\n"
    "  }\n"
    "  out.sort((a, b) => (a.start < b.start ? -1 : 1));\n"
    "  return JSON.stringify(out);\n"
    "}"
)

_CREATE_DESCRIPTION = (
    "Create an event on the owner's Apple Calendar (Calendar.app — distinct from "
    "Google Calendar). Start/end are ISO date-times in the owner's local time. Name "
    "the target calendar when known; the first calendar is used when omitted. Needs "
    "the owner's approval."
)
_LIST_DESCRIPTION = (
    "List the owner's Apple Calendar events in a time window (ISO start/end, "
    "owner-local). Optionally scope to one named calendar; every calendar when "
    "omitted."
)

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Event title."},
        "start": {"type": "string", "description": "Start, ISO, owner-local."},
        "end": {"type": "string", "description": "End, ISO, owner-local."},
        "calendar": {"type": "string", "description": "Optional calendar name."},
        "location": {"type": "string", "description": "Optional location."},
        "notes": {"type": "string", "description": "Optional description."},
    },
    "required": ["summary", "start", "end"],
}
_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "Window start, ISO."},
        "end": {"type": "string", "description": "Window end, ISO."},
        "calendar": {"type": "string", "description": "Optional calendar name."},
    },
    "required": ["start", "end"],
}


def _format_events(raw: str) -> str:
    """Render the LIST_SCRIPT JSON as a readable agenda."""
    events = json.loads(raw) if raw.strip() else []
    if not events:
        return "No events in that window."
    lines = []
    for ev in events:
        location = f" @ {ev['location']}" if ev.get("location") else ""
        lines.append(
            f"- {ev.get('start', '?')} – {ev.get('end', '?')}: "
            f"{ev.get('summary', '')}{location} ({ev.get('calendar', '')})"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class AppleCalendarService:
    """Builds the owner session's Apple Calendar server (create/list events)."""

    runner: ScriptRunner
    server_name: str = SERVER_NAME
    capability: str = "calendar"

    def _build_create(self) -> InProcessTool:
        runner = self.runner

        @tool("create_event", _CREATE_DESCRIPTION, _CREATE_SCHEMA)
        async def create_event(args: dict[str, Any]) -> dict[str, Any]:
            summary = str(args.get("summary", "")).strip()
            start = str(args.get("start", "")).strip()
            end = str(args.get("end", "")).strip()
            if not (summary and start and end):
                return text_result(
                    "An event needs a summary, a start, and an end.", is_error=True
                )
            result = await runner.run_jxa(
                CREATE_SCRIPT,
                [
                    summary,
                    start,
                    end,
                    str(args.get("calendar", "")).strip(),
                    str(args.get("location", "")).strip(),
                    str(args.get("notes", "")).strip(),
                ],
            )
            if not result.ok:
                return script_error_result("create the event", result)
            return text_result(f"Event {summary!r} {result.stdout.strip()}.")

        return create_event

    def _build_list(self) -> InProcessTool:
        runner = self.runner

        @tool("list_events", _LIST_DESCRIPTION, _LIST_SCHEMA)
        async def list_events(args: dict[str, Any]) -> dict[str, Any]:
            start = str(args.get("start", "")).strip()
            end = str(args.get("end", "")).strip()
            if not (start and end):
                return text_result(
                    "Give the window's start and end (ISO).", is_error=True
                )
            result = await runner.run_jxa(
                LIST_SCRIPT, [start, end, str(args.get("calendar", "")).strip()]
            )
            if not result.ok:
                return script_error_result("list events", result)
            try:
                return text_result(_format_events(result.stdout))
            except ValueError:
                return text_result(
                    f"Calendar returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return list_events

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Apple Calendar tools."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_create(), self._build_list()]
        )
