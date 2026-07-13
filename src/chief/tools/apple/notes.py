"""Owner-only Apple Notes tools (#155): create, search, read.

An in-process MCP server (``chief_apple_notes``) driving Notes.app through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam (fixed JXA; owner data as argv).
Note bodies are HTML in Notes' scripting dictionary, so create escapes the plain text
inside the script and derives the note's title from a leading ``<h1>`` (the behavior
Notes itself uses). All three tools are owner-local and non-destructive — no
blacklist seeding.
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

SERVER_NAME = "chief_apple_notes"
CREATE_TOOL = f"mcp__{SERVER_NAME}__create_note"
SEARCH_TOOL = f"mcp__{SERVER_NAME}__search_notes"
READ_TOOL = f"mcp__{SERVER_NAME}__read_note"

#: argv: [title, plain-text body, folder-name] (empty folder = default). The title is
#: emitted as a leading <h1> so Notes derives the note name from it; body lines become
#: <div> paragraphs. Escaping happens in JS against the argv values.
CREATE_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Notes');\n"
    "  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')\n"
    "                      .replace(/>/g, '&gt;');\n"
    "  const paras = argv[1].split('\\n')\n"
    "    .map((l) => '<div>' + (l ? esc(l) : '<br>') + '</div>').join('');\n"
    "  const html = '<div><h1>' + esc(argv[0]) + '</h1></div>' + paras;\n"
    "  const note = app.Note({body: html});\n"
    "  if (argv[2]) { app.folders.byName(argv[2]).notes.push(note); }\n"
    "  else { app.notes.push(note); }\n"
    "  return 'created';\n"
    "}"
)

#: argv: [query]. Case-insensitive substring match on name OR plaintext body; returns
#: JSON with a short plaintext snippet per hit (capped to keep output bounded).
SEARCH_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Notes');\n"
    "  const hits = app.notes.whose({_or: [\n"
    "    {name: {_contains: argv[0]}},\n"
    "    {plaintext: {_contains: argv[0]}}\n"
    "  ]})();\n"
    "  const out = hits.slice(0, 20).map((n) => ({\n"
    "    name: n.name(),\n"
    "    snippet: n.plaintext().slice(0, 200),\n"
    "    modified: n.modificationDate().toISOString()\n"
    "  }));\n"
    "  return JSON.stringify(out);\n"
    "}"
)

#: argv: [exact-name]. Returns the note's full plaintext (bounded by the runner cap).
READ_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application('Notes');\n"
    "  const hits = app.notes.whose({name: argv[0]})();\n"
    "  if (hits.length === 0) { return 'NOT FOUND'; }\n"
    "  return hits[0].plaintext();\n"
    "}"
)

_CREATE_DESCRIPTION = (
    "Create an Apple Note with a title and plain-text body. Optionally file it in a "
    "named Notes folder (the default folder when omitted)."
)
_SEARCH_DESCRIPTION = (
    "Search the owner's Apple Notes by substring (title and body text). Returns the "
    "matching notes' titles, a snippet, and last-modified time. Follow up with "
    "read_note for a full note."
)
_READ_DESCRIPTION = (
    "Read the full plain text of one Apple Note by its exact title (use "
    "search_notes first if unsure)."
)

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The note's title."},
        "body": {"type": "string", "description": "Plain-text note body."},
        "folder": {"type": "string", "description": "Optional Notes folder name."},
    },
    "required": ["title", "body"],
}
_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to search for."},
    },
    "required": ["query"],
}
_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The note's exact title."},
    },
    "required": ["title"],
}


def _format_hits(raw: str) -> str:
    """Render the SEARCH_SCRIPT JSON as a readable result list."""
    hits = json.loads(raw) if raw.strip() else []
    if not hits:
        return "No notes matched."
    lines = []
    for hit in hits:
        snippet = " ".join(str(hit.get("snippet", "")).split())
        lines.append(
            f"- {hit.get('name', '')} (modified {hit.get('modified', '?')})\n"
            f"  {snippet}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class NotesService:
    """Builds the owner session's Apple Notes server (create/search/read)."""

    runner: ScriptRunner
    server_name: str = SERVER_NAME
    capability: str = "notes"

    def _build_create(self) -> InProcessTool:
        runner = self.runner

        @tool("create_note", _CREATE_DESCRIPTION, _CREATE_SCHEMA)
        async def create_note(args: dict[str, Any]) -> dict[str, Any]:
            title = str(args.get("title", "")).strip()
            body = str(args.get("body", ""))
            if not title:
                return text_result("A note needs a title.", is_error=True)
            result = await runner.run_jxa(
                CREATE_SCRIPT, [title, body, str(args.get("folder", "")).strip()]
            )
            if not result.ok:
                return script_error_result("create the note", result)
            return text_result(f"Note {title!r} created.")

        return create_note

    def _build_search(self) -> InProcessTool:
        runner = self.runner

        @tool("search_notes", _SEARCH_DESCRIPTION, _SEARCH_SCHEMA)
        async def search_notes(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query", "")).strip()
            if not query:
                return text_result("No search query provided.", is_error=True)
            result = await runner.run_jxa(SEARCH_SCRIPT, [query])
            if not result.ok:
                return script_error_result("search Notes", result)
            try:
                return text_result(_format_hits(result.stdout))
            except ValueError:
                return text_result(
                    f"Notes returned unparseable output: {result.stdout[:200]}",
                    is_error=True,
                )

        return search_notes

    def _build_read(self) -> InProcessTool:
        runner = self.runner

        @tool("read_note", _READ_DESCRIPTION, _READ_SCHEMA)
        async def read_note(args: dict[str, Any]) -> dict[str, Any]:
            title = str(args.get("title", "")).strip()
            if not title:
                return text_result("Which note? Give its exact title.", is_error=True)
            result = await runner.run_jxa(READ_SCRIPT, [title])
            if not result.ok:
                return script_error_result("read the note", result)
            if result.stdout.strip() == "NOT FOUND":
                return text_result(
                    f"No note titled {title!r} — use search_notes to find the "
                    "exact title.",
                    is_error=True,
                )
            return text_result(result.stdout.strip() or "(the note is empty)")

        return read_note

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Notes tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[self._build_create(), self._build_search(), self._build_read()],
        )
