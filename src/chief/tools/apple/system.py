"""Owner-only macOS system-control tools (#155): clipboard, notifications,
screenshots.

An in-process MCP server (``chief_apple_system``) driving ``pbpaste``/``pbcopy``,
``osascript`` (user notifications), and ``screencapture`` through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam. All four tools are owner-local
(nothing reaches other people or destroys data), so none is blacklist-seeded.

Screenshots land as PNG files under ``screenshots_dir`` and the tool returns the
path; note that macOS additionally gates *content* capture behind the Screen
Recording grant — without it ``screencapture`` still succeeds but captures only the
wallpaper (the doctor's checklist calls this out, since no probe can detect it).
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..inprocess import (
    InProcessServerConfig,
    InProcessTool,
    create_sdk_mcp_server,
    tool,
)
from .runner import ENCODING, ScriptRunner, script_error_result, text_result

SERVER_NAME = "chief_apple_system"
CLIPBOARD_READ_TOOL = f"mcp__{SERVER_NAME}__clipboard_read"
CLIPBOARD_WRITE_TOOL = f"mcp__{SERVER_NAME}__clipboard_write"
NOTIFY_TOOL = f"mcp__{SERVER_NAME}__notify"
SCREENSHOT_TOOL = f"mcp__{SERVER_NAME}__screenshot"

#: argv: [message, title]. Posts a standard macOS user notification.
NOTIFY_SCRIPT = (
    "function run(argv) {\n"
    "  const app = Application.currentApplication();\n"
    "  app.includeStandardAdditions = true;\n"
    "  app.displayNotification(argv[0], {withTitle: argv[1]});\n"
    "  return 'ok';\n"
    "}"
)

_CLIPBOARD_READ_DESCRIPTION = (
    "Read the current text content of the owner's macOS clipboard."
)
_CLIPBOARD_WRITE_DESCRIPTION = (
    "Replace the owner's macOS clipboard with the given text."
)
_NOTIFY_DESCRIPTION = (
    "Show a macOS user notification on the owner's screen (a message with a title)."
)
_SCREENSHOT_DESCRIPTION = (
    "Capture the owner's whole screen to a PNG file and return the file path. "
    "Silent (no shutter sound)."
)

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
_CLIPBOARD_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Text to place on the clipboard."},
    },
    "required": ["text"],
}
_NOTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "Notification body."},
        "title": {"type": "string", "description": "Notification title."},
    },
    "required": ["message", "title"],
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class SystemService:
    """Builds the owner session's macOS system-control server.

    ``screenshots_dir`` is where captures land (shared with the browser screenshot
    dir, wired in :mod:`chief.app`); file names are ``apple-<epoch-ms>.png``.
    """

    runner: ScriptRunner
    screenshots_dir: str
    server_name: str = SERVER_NAME
    capability: str = "system"

    def _build_clipboard_read(self) -> InProcessTool:
        runner = self.runner

        @tool("clipboard_read", _CLIPBOARD_READ_DESCRIPTION, _EMPTY_SCHEMA)
        async def clipboard_read(args: dict[str, Any]) -> dict[str, Any]:
            result = await runner.run([runner.pbpaste_path])
            if not result.ok:
                return script_error_result("read the clipboard", result)
            return text_result(result.stdout or "(the clipboard is empty)")

        return clipboard_read

    def _build_clipboard_write(self) -> InProcessTool:
        runner = self.runner

        @tool("clipboard_write", _CLIPBOARD_WRITE_DESCRIPTION,
              _CLIPBOARD_WRITE_SCHEMA)
        async def clipboard_write(args: dict[str, Any]) -> dict[str, Any]:
            text = str(args.get("text", ""))
            result = await runner.run(
                [runner.pbcopy_path], stdin=text.encode(ENCODING)
            )
            if not result.ok:
                return script_error_result("write the clipboard", result)
            return text_result("Clipboard updated.")

        return clipboard_write

    def _build_notify(self) -> InProcessTool:
        runner = self.runner

        @tool("notify", _NOTIFY_DESCRIPTION, _NOTIFY_SCHEMA)
        async def notify(args: dict[str, Any]) -> dict[str, Any]:
            message = str(args.get("message", "")).strip()
            title = str(args.get("title", "")).strip() or "chief"
            if not message:
                return text_result("A notification needs a message.", is_error=True)
            result = await runner.run_jxa(NOTIFY_SCRIPT, [message, title])
            if not result.ok:
                return script_error_result("show the notification", result)
            return text_result("Notification shown.")

        return notify

    def _build_screenshot(self) -> InProcessTool:
        runner = self.runner
        screenshots_dir = self.screenshots_dir

        @tool("screenshot", _SCREENSHOT_DESCRIPTION, _EMPTY_SCHEMA)
        async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
            directory = Path(screenshots_dir)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"apple-{_now_ms()}.png"
            # -x silences the shutter sound (a screenshot shouldn't announce itself).
            result = await runner.run(
                [runner.screencapture_path, "-x", str(path)]
            )
            if not result.ok:
                return script_error_result("take the screenshot", result)
            return text_result(f"Screenshot saved to {path}.")

        return screenshot

    def server_config(self) -> InProcessServerConfig:
        """The in-process ``mcp_servers`` entry for the system-control tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[
                self._build_clipboard_read(),
                self._build_clipboard_write(),
                self._build_notify(),
                self._build_screenshot(),
            ],
        )
