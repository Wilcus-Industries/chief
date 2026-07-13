"""Owner-only Apple Reminders tools (#155): create, list, complete.

An in-process MCP server (``chief_apple_reminders``) built the
:mod:`chief.tools.web` way, driving Reminders.app through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam (fixed JXA scripts; owner data
as osascript argv). All three tools are owner-local and non-destructive, so none is
blacklist-seeded — they ALLOW freely under the owner default-allow gate.
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

SERVER_NAME = "chief_apple_reminders"
CREATE_TOOL = f"mcp__{SERVER_NAME}__create_reminder"
LIST_TOOL = f"mcp__{SERVER_NAME}__list_reminders"
COMPLETE_TOOL = f"mcp__{SERVER_NAME}__complete_reminder"

#: argv: [name, notes, due-ISO, list-name] (empty string = omitted). Setting both
#: remindMeDate and dueDate makes the alert actually fire on the owner's devices.
CREATE_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Reminders');\n"
    "  const props = {name: argv[0]};\n"
    "  if (argv[1]) props.body = argv[1];\n"
    "  if (argv[2]) {\n"
    "    const due = new Date(argv[2]);\n"
    "    props.remindMeDate = due;\n"
    "    props.dueDate = due;\n"
    "  }\n"
    "  const list = argv[3] ? app.lists.byName(argv[3]) : app.defaultList();\n"
    "  list.reminders.push(app.Reminder(props));\n"
    "  return 'created in ' + list.name();\n"
    "}"
)

#: argv: [list-name] (empty = every list). Returns the open reminders as JSON.
LIST_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Reminders');\n"
    "  const lists = argv[0] ? [app.lists.byName(argv[0])] : app.lists();\n"
    "  const out = [];\n"
    "  for (const list of lists) {\n"
    "    const open = list.reminders.whose({completed: false})();\n"
    "    for (const r of open) {\n"
    "      const due = r.dueDate();\n"
    "      out.push({name: r.name(), body: r.body(), list: list.name(),\n"
    "                due: due ? due.toISOString() : null});\n"
    "    }\n"
    "  }\n"
    "  return JSON.stringify(out);\n"
    "}"
)

#: argv: [exact-name, list-name] (empty list = search every list). Completes the
#: first open reminder whose name matches exactly.
COMPLETE_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Reminders');\n"
    "  const lists = argv[1] ? [app.lists.byName(argv[1])] : app.lists();\n"
    "  for (const list of lists) {\n"
    "    const open = list.reminders.whose({name: argv[0], completed: false})();\n"
    "    if (open.length > 0) {\n"
    "      open[0].completed = true;\n"
    "      return 'completed';\n"
    "    }\n"
    "  }\n"
    "  return 'not found';\n"
    "}"
)

_CREATE_DESCRIPTION = (
    "Create an Apple Reminder on the owner's devices. Give it a name, and optionally "
    "a due date-time (ISO format, owner-local, e.g. 2026-07-14T08:00:00), notes, and "
    "a Reminders list name (the default list when omitted)."
)
_LIST_DESCRIPTION = (
    "List the owner's open (incomplete) Apple Reminders. Optionally scope to one "
    "Reminders list by name; every list when omitted."
)
_COMPLETE_DESCRIPTION = (
    "Mark an open Apple Reminder as completed, by its exact name (use "
    "list_reminders first if unsure). Optionally scope to one list by name."
)

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The reminder text."},
        "due": {
            "type": "string",
            "description": "Optional due date-time, ISO, owner-local.",
        },
        "notes": {"type": "string", "description": "Optional notes body."},
        "list": {"type": "string", "description": "Optional Reminders list name."},
    },
    "required": ["name"],
}
_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "list": {"type": "string", "description": "Optional Reminders list name."},
    },
    "required": [],
}
_COMPLETE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The reminder's exact name."},
        "list": {"type": "string", "description": "Optional Reminders list name."},
    },
    "required": ["name"],
}


def _format_listing(raw: str) -> str:
    """Render the LIST_SCRIPT JSON as a readable bullet list."""
    items = json.loads(raw) if raw.strip() else []
    if not items:
        return "No open reminders."
    lines = []
    for item in items:
        parts = [str(item.get("name", ""))]
        if item.get("due"):
            parts.append(f"due {item['due']}")
        if item.get("body"):
            parts.append(str(item["body"]))
        lines.append(f"- {parts[0]} ({item.get('list', '')})" + (
            f" — {'; '.join(parts[1:])}" if len(parts) > 1 else ""
        ))
    return "\n".join(lines)


@dataclass(frozen=True)
class RemindersService:
    """Builds the owner session's Apple Reminders server (create/list/complete)."""

    runner: ScriptRunner
    server_name: str = SERVER_NAME
    capability: str = "reminders"

    def _build_create(self) -> InProcessTool:
        runner = self.runner

        @tool("create_reminder", _CREATE_DESCRIPTION, _CREATE_SCHEMA)
        async def create_reminder(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip()
            if not name:
                return text_result("A reminder needs a name.", is_error=True)
            result = await runner.run_jxa(
                CREATE_SCRIPT,
                [
                    name,
                    str(args.get("notes", "")).strip(),
                    str(args.get("due", "")).strip(),
                    str(args.get("list", "")).strip(),
                ],
            )
            if not result.ok:
                return script_error_result("create the reminder", result)
            return text_result(f"Reminder {name!r} {result.stdout.strip()}.")

        return create_reminder

    def _build_list(self) -> InProcessTool:
        runner = self.runner

        @tool("list_reminders", _LIST_DESCRIPTION, _LIST_SCHEMA)
        async def list_reminders(args: dict[str, Any]) -> dict[str, Any]:
            result = await runner.run_jxa(
                LIST_SCRIPT, [str(args.get("list", "")).strip()]
            )
            if not result.ok:
                return script_error_result("list reminders", result)
            try:
                return text_result(_format_listing(result.stdout))
            except ValueError:
                return text_result(
                    f"Reminders returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return list_reminders

    def _build_complete(self) -> InProcessTool:
        runner = self.runner

        @tool("complete_reminder", _COMPLETE_DESCRIPTION, _COMPLETE_SCHEMA)
        async def complete_reminder(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip()
            if not name:
                return text_result("Which reminder? Give its exact name.",
                                   is_error=True)
            result = await runner.run_jxa(
                COMPLETE_SCRIPT, [name, str(args.get("list", "")).strip()]
            )
            if not result.ok:
                return script_error_result("complete the reminder", result)
            if result.stdout.strip() == "completed":
                return text_result(f"Completed {name!r}.")
            return text_result(
                f"No open reminder named {name!r} — use list_reminders to see "
                "the exact names.",
                is_error=True,
            )

        return complete_reminder

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Reminders tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[self._build_create(), self._build_list(), self._build_complete()],
        )
