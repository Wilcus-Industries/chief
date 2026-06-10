"""Screenshot delivery: path mapping + PostToolUse hook for browser_take_screenshot.

When the playwright server takes a screenshot it saves the file to its
``--output-dir`` (the shared ``screenshots`` volume) and returns the filename in
the tool result text as a markdown link:

    ``- [Screenshot of viewport](page-1718000000000.png)``

:func:`extract_screenshot_filename` parses that filename out of the tool
response.  :func:`screenshot_path` maps it to the absolute path inside core (the
volume is mounted at the same path in both containers).  :func:`build_screenshot_hook`
assembles a ``PostToolUse`` ``HookCallback`` that wires those two functions to the
existing :class:`~chief.core.tasks.TaskIO` ``send_file`` delivery path.
"""

import logging
import re
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk.types import HookCallback, HookContext

logger = logging.getLogger("chief.tools.browser.screenshot")

#: Mount point for the shared screenshots volume inside every container
#: (mcp-playwright writes here; core reads here).  Must match the compose
#: ``volumes:`` entries for both services.
SCREENSHOTS_DIR = "/screenshots"

#: Qualified tool name for the playwright screenshot tool.
_SCREENSHOT_TOOL = "mcp__playwright__browser_take_screenshot"

#: Regex to extract a filename from a playwright markdown file-link:
#: "- [Title](filename.ext)"  where ext is a common image format.
_LINK_RE = re.compile(r"\(([^)]+\.(?:png|jpeg|jpg|webp))\)", re.IGNORECASE)


class _IOProto(Protocol):
    """The slice of TaskIO the screenshot hook needs."""

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None: ...


def screenshot_path(filename: str, screenshots_dir: str = SCREENSHOTS_DIR) -> Path:
    """Map a screenshot *filename* to its absolute path inside core.

    Strips any directory component from *filename* (the playwright server uses
    relative names, but a path separator could otherwise escape the volume root).
    """
    return Path(screenshots_dir) / Path(filename).name


def extract_screenshot_filename(text: str) -> str | None:
    """Return the first image filename from a playwright markdown tool-result text.

    Returns ``None`` when no ``(filename.ext)`` link is found.
    """
    match = _LINK_RE.search(text)
    return match.group(1) if match else None


def _tool_response_text(tool_response: Any) -> str:
    """Extract the concatenated text from a tool response's content list."""
    if not isinstance(tool_response, dict):
        return ""
    parts: list[str] = []
    for item in tool_response.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


def build_screenshot_hook(
    *,
    thread_key: str,
    io: _IOProto,
    screenshots_dir: str = SCREENSHOTS_DIR,
) -> HookCallback:
    """A ``PostToolUse`` hook that delivers screenshots via :meth:`TaskIO.send_file`.

    On every ``browser_take_screenshot`` tool result the hook:

    1. Parses the filename from the playwright markdown link in the tool response.
    2. Resolves the absolute path inside core via :func:`screenshot_path`.
    3. Reads the file and calls ``io.send_file`` to deliver it to the owner.

    Missing files (e.g. a race against the server writing the file) are logged and
    skipped — the model's text reply still reaches the owner.
    """

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        if tool_name != _SCREENSHOT_TOOL:
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}

        response_text = _tool_response_text(input_data.get("tool_response", {}))
        filename = extract_screenshot_filename(response_text)
        if filename is None:
            logger.debug(
                "browser_take_screenshot: no filename found in tool response"
            )
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}

        path = screenshot_path(filename, screenshots_dir)
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning(
                "browser_take_screenshot: screenshot file not readable: %s", path
            )
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}

        await io.send_file(thread_key, Path(filename).name, data, caption=None)
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}

    from typing import cast

    return cast(HookCallback, hook)
