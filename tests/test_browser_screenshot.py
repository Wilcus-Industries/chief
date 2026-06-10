"""Unit tests for screenshot path-mapping and the PostToolUse delivery hook."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chief.tools.browser.screenshot import (
    SCREENSHOTS_DIR,
    build_screenshot_hook,
    extract_screenshot_filename,
    screenshot_path,
)

# The SDK's HookCallback uses strict input/output unions; tests drive hooks
# via their runtime shape, so we use this loose alias for invocation.
_HookFn = Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]]

# ---- path mapping tests --------------------------------------------------------


def test_screenshot_path_joins_dir_and_filename() -> None:
    # The path mapping must join the screenshots dir and the bare filename.
    path = screenshot_path("page-12345.png", screenshots_dir="/screenshots")
    assert path == Path("/screenshots/page-12345.png")


def test_screenshot_path_uses_default_dir() -> None:
    # When no screenshots_dir is given, the default constant is used.
    path = screenshot_path("page-99.png")
    assert path == Path(SCREENSHOTS_DIR) / "page-99.png"


def test_screenshot_path_basename_only_strips_any_directory() -> None:
    # A filename with a subdirectory component must be reduced to its basename
    # so a relative path in the tool result can't escape the screenshots dir.
    path = screenshot_path("subdir/page-1.png", screenshots_dir="/screenshots")
    assert path == Path("/screenshots/page-1.png")


# ---- filename extraction tests -------------------------------------------------


def test_extract_filename_from_markdown_link() -> None:
    # Standard playwright output format: - [Title](filename.png)
    text = "- [Screenshot of viewport](page-1718000000000.png)"
    assert extract_screenshot_filename(text) == "page-1718000000000.png"


def test_extract_filename_returns_none_when_no_link() -> None:
    # When the tool response text doesn't contain a markdown link, return None.
    assert extract_screenshot_filename("No screenshot taken.") is None


def test_extract_filename_picks_first_png_link() -> None:
    # Multiple links — the first image filename wins.
    text = (
        "- [Screenshot of viewport](page-111.png)\n"
        "- [Other](page-222.png)"
    )
    assert extract_screenshot_filename(text) == "page-111.png"


def test_extract_filename_handles_jpeg() -> None:
    text = "- [Screenshot of element](element-1234.jpeg)"
    assert extract_screenshot_filename(text) == "element-1234.jpeg"


# ---- PostToolUse hook tests ----------------------------------------------------


def _make_tool_response(filename: str, *, with_image: bool = False) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"- [Screenshot of viewport]({filename})"}
    ]
    if with_image:
        import base64

        content.append(
            {
                "type": "image",
                "data": base64.b64encode(b"fake-png-bytes").decode(),
                "mimeType": "image/png",
            }
        )
    return {"content": content}


def _as_callable(hook: Any) -> _HookFn:
    """Cast a HookCallback to the loose test-callable alias."""
    return hook  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_hook_reads_file_and_calls_send_file(tmp_path: Path) -> None:
    # The hook must read the screenshot file from the screenshots dir and deliver
    # it via send_file on the IO.
    screenshots_dir = str(tmp_path)
    filename = "page-1234.png"
    (tmp_path / filename).write_bytes(b"\x89PNG\r\nfake")

    io = AsyncMock()
    hook = _as_callable(
        build_screenshot_hook(
            thread_key="-100:5",
            io=io,
            screenshots_dir=screenshots_dir,
        )
    )

    input_data: dict[str, Any] = {
        "tool_name": "mcp__playwright__browser_take_screenshot",
        "tool_response": _make_tool_response(filename),
    }
    result = await hook(input_data, None, {"signal": None})

    io.send_file.assert_awaited_once()
    call_kwargs = io.send_file.call_args
    assert call_kwargs.args[0] == "-100:5"  # thread_key
    assert call_kwargs.args[1] == filename
    assert call_kwargs.args[2] == b"\x89PNG\r\nfake"
    assert result == {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}


@pytest.mark.asyncio
async def test_hook_ignores_non_screenshot_tools(tmp_path: Path) -> None:
    io = AsyncMock()
    hook = _as_callable(
        build_screenshot_hook(
            thread_key="-100:5",
            io=io,
            screenshots_dir=str(tmp_path),
        )
    )

    input_data: dict[str, Any] = {
        "tool_name": "mcp__playwright__browser_navigate",
        "tool_response": {"content": [{"type": "text", "text": "OK"}]},
    }
    await hook(input_data, None, {"signal": None})
    io.send_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_skips_delivery_when_file_missing(tmp_path: Path) -> None:
    # If the screenshot file is not present (e.g. race with the server), the hook
    # must not raise — it logs and returns silently.
    io = AsyncMock()
    hook = _as_callable(
        build_screenshot_hook(
            thread_key="-100:5",
            io=io,
            screenshots_dir=str(tmp_path),
        )
    )

    input_data: dict[str, Any] = {
        "tool_name": "mcp__playwright__browser_take_screenshot",
        "tool_response": _make_tool_response("missing-file.png"),
    }
    result = await hook(input_data, None, {"signal": None})
    io.send_file.assert_not_awaited()
    assert result == {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}


@pytest.mark.asyncio
async def test_hook_skips_when_no_filename_in_response(tmp_path: Path) -> None:
    io = AsyncMock()
    hook = _as_callable(
        build_screenshot_hook(
            thread_key="-100:5",
            io=io,
            screenshots_dir=str(tmp_path),
        )
    )

    input_data: dict[str, Any] = {
        "tool_name": "mcp__playwright__browser_take_screenshot",
        "tool_response": {
            "content": [{"type": "text", "text": "Error taking screenshot."}]
        },
    }
    await hook(input_data, None, {"signal": None})
    io.send_file.assert_not_awaited()
