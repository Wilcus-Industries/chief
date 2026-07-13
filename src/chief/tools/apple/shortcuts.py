"""Owner-only Apple Shortcuts tools (#155): list and run.

An in-process MCP server (``chief_apple_shortcuts``) driving the ``shortcuts`` CLI
through the :class:`~chief.tools.apple.runner.ScriptRunner` seam. This is the escape
hatch to everything Apple ships and the owner has automated — which is exactly why
``run_shortcut`` is seeded into ``blacklist_tools`` (:mod:`chief.config`): an
arbitrary shortcut can send, delete, or reach anything, so the first run of each
raises an approval card until the owner approves the shape into the APPROVED list.
``list_shortcuts`` reads freely.
"""

from dataclasses import dataclass
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .runner import ENCODING, ScriptRunner, script_error_result, text_result

SERVER_NAME = "chief_apple_shortcuts"
LIST_TOOL = f"mcp__{SERVER_NAME}__list_shortcuts"
RUN_TOOL = f"mcp__{SERVER_NAME}__run_shortcut"

#: Running an arbitrary shortcut ASKs (approval card) — the PRD's "runs an arbitrary
#: shortcut" case. Seeded into ``blacklist_tools`` by :mod:`chief.config`.
MUTATING_TOOL_NAMES: tuple[str, ...] = (RUN_TOOL,)

_LIST_DESCRIPTION = (
    "List the shortcuts installed in the owner's Shortcuts app, one name per line."
)
_RUN_DESCRIPTION = (
    "Run one of the owner's installed shortcuts by exact name (use list_shortcuts "
    "first if unsure), optionally passing a text input. Returns the shortcut's "
    "output. Needs the owner's approval."
)

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}
_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The shortcut's exact name."},
        "input": {"type": "string", "description": "Optional text input."},
    },
    "required": ["name"],
}


@dataclass(frozen=True)
class ShortcutsService:
    """Builds the owner session's Shortcuts server (list + gated run)."""

    runner: ScriptRunner
    server_name: str = SERVER_NAME
    capability: str = "shortcuts"

    def _build_list(self) -> InProcessTool:
        runner = self.runner

        @tool("list_shortcuts", _LIST_DESCRIPTION, _LIST_SCHEMA)
        async def list_shortcuts(args: dict[str, Any]) -> dict[str, Any]:
            result = await runner.run_shortcuts(["list"])
            if not result.ok:
                return script_error_result("list shortcuts", result)
            names = result.stdout.strip()
            return text_result(names or "No shortcuts installed.")

        return list_shortcuts

    def _build_run(self) -> InProcessTool:
        runner = self.runner

        @tool("run_shortcut", _RUN_DESCRIPTION, _RUN_SCHEMA)
        async def run_shortcut(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip()
            if not name:
                return text_result("Which shortcut? Give its exact name.",
                                   is_error=True)
            text_input = str(args.get("input", ""))
            cli_args = ["run", name, "-o", "-"]
            stdin: bytes | None = None
            if text_input:
                # "-" reads the shortcut's input from stdin (Shortcuts CLI contract).
                cli_args += ["-i", "-"]
                stdin = text_input.encode(ENCODING)
            result = await runner.run_shortcuts(cli_args, stdin=stdin)
            if not result.ok:
                return script_error_result(f"run the shortcut {name!r}", result)
            output = result.stdout.strip()
            return text_result(
                f"Shortcut {name!r} ran.\n{output}" if output
                else f"Shortcut {name!r} ran (no output)."
            )

        return run_shortcut

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the Shortcuts tools."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_list(), self._build_run()]
        )
