"""Owner-only Apple Contacts lookup (#155): name → handles/emails/numbers.

An in-process MCP server (``chief_apple_contacts``) driving Contacts.app through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam. One read-only tool — the lookup
the other Apple tools and the future iMessage adapter lean on (a phone number or
email resolved here feeds a Messages history search there). Never blacklist-seeded.
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

SERVER_NAME = "chief_apple_contacts"
LOOKUP_TOOL = f"mcp__{SERVER_NAME}__lookup_contact"

#: argv: [name substring]. Returns up to 10 matches with labelled phones + emails.
LOOKUP_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Contacts');\n"
    "  const people = app.people.whose({name: {_contains: argv[0]}})();\n"
    "  const out = people.slice(0, 10).map((p) => ({\n"
    "    name: p.name(),\n"
    "    phones: p.phones().map((ph) => ({label: ph.label(),\n"
    "                                     number: ph.value()})),\n"
    "    emails: p.emails().map((e) => ({label: e.label(),\n"
    "                                    address: e.value()}))\n"
    "  }));\n"
    "  return JSON.stringify(out);\n"
    "}"
)

_LOOKUP_DESCRIPTION = (
    "Look up a person in the owner's Apple Contacts by (partial) name. Returns each "
    "match's phone numbers and email addresses with their labels (mobile, home, "
    "work, ...)."
)

_LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Name (or part of it) to find."},
    },
    "required": ["name"],
}


def _format_people(raw: str) -> str:
    """Render the LOOKUP_SCRIPT JSON as readable contact cards."""
    people = json.loads(raw) if raw.strip() else []
    if not people:
        return "No contacts matched."
    lines = []
    for person in people:
        lines.append(f"{person.get('name', '')}:")
        for phone in person.get("phones", []):
            label = str(phone.get("label") or "phone")
            lines.append(f"  - {label}: {phone.get('number', '')}")
        for email in person.get("emails", []):
            label = str(email.get("label") or "email")
            lines.append(f"  - {label}: {email.get('address', '')}")
        if not person.get("phones") and not person.get("emails"):
            lines.append("  - (no phone or email on file)")
    return "\n".join(lines)


@dataclass(frozen=True)
class ContactsService:
    """Builds the owner session's Apple Contacts server (lookup only)."""

    runner: ScriptRunner
    server_name: str = SERVER_NAME
    capability: str = "contacts"

    def _build_lookup(self) -> InProcessTool:
        runner = self.runner

        @tool("lookup_contact", _LOOKUP_DESCRIPTION, _LOOKUP_SCHEMA)
        async def lookup_contact(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip()
            if not name:
                return text_result("Whose contact? Give a name.", is_error=True)
            result = await runner.run_jxa(LOOKUP_SCRIPT, [name])
            if not result.ok:
                return script_error_result("look up the contact", result)
            try:
                return text_result(_format_people(result.stdout))
            except ValueError:
                return text_result(
                    f"Contacts returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return lookup_contact

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Contacts lookup."""
        return create_sdk_mcp_server(self.server_name, tools=[self._build_lookup()])
