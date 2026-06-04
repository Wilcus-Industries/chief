"""The core-side bash tool + socket client, against a real local shell server.

Starts a genuine :class:`ShellServer` on an ephemeral port (workdir under tmp_path),
then drives the in-process tool the way the SDK would — through its handler — asserting
it forwards to the sandbox and that two tasks' session keys reach isolated shells.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from chief.sandbox.shell_server import ShellServer
from chief.tools.shell import TOOL_NAME, ShellService, format_result, run_command


@pytest_asyncio.fixture
async def shell(tmp_path: Path) -> AsyncIterator[ShellService]:
    """A ShellService pointed at a real local ShellServer rooted at tmp_path."""
    server = ShellServer(timeout=2.0, output_limit=10_000, workdir=str(tmp_path))
    tcp = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    service = ShellService(
        host="127.0.0.1", port=port, timeout_seconds=2.0, output_limit=10_000
    )
    try:
        yield service
    finally:
        await server.aclose()
        tcp.close()
        await tcp.wait_closed()


def test_tool_name_is_sdk_qualified() -> None:
    service = ShellService(host="h", port=1, timeout_seconds=1.0, output_limit=1)
    assert service.tool_name == TOOL_NAME == "mcp__chief_shell__bash"


def test_format_result_marks_errors_and_truncation() -> None:
    ok = format_result(
        {"stdout": "hi", "stderr": "", "exit_code": 0, "truncated": False}
    )
    assert ok["is_error"] is False
    assert ok["content"][0]["text"] == "hi"

    bad = format_result(
        {"stdout": "", "stderr": "boom", "exit_code": 2, "truncated": True}
    )
    assert bad["is_error"] is True
    text = bad["content"][0]["text"]
    assert "boom" in text and "exit code 2" in text and "truncated" in text


async def test_run_command_round_trip(shell: ShellService) -> None:
    result = await run_command(
        shell.host, shell.port, "s1", "echo hello", read_timeout=5.0
    )
    assert result["stdout"] == "hello"
    assert result["exit_code"] == 0


async def test_bash_tool_forwards_to_sandbox(shell: ShellService) -> None:
    bash = shell._build_bash_tool("t1")
    out = await bash.handler({"command": "echo from-tool"})

    assert out["is_error"] is False
    assert out["content"][0]["text"] == "from-tool"


async def test_session_keys_isolate_two_tasks(shell: ShellService) -> None:
    task_a = shell._build_bash_tool("task-a")
    task_b = shell._build_bash_tool("task-b")

    await task_a.handler({"command": "export WHO=alice"})
    await task_b.handler({"command": "export WHO=bob"})

    a = await task_a.handler({"command": "echo $WHO"})
    b = await task_b.handler({"command": "echo $WHO"})

    assert a["content"][0]["text"] == "alice"
    assert b["content"][0]["text"] == "bob"


async def test_bash_tool_reports_sandbox_unavailable() -> None:
    # Point at a closed port: the tool surfaces the failure instead of raising.
    service = ShellService(
        host="127.0.0.1", port=1, timeout_seconds=1.0, output_limit=1
    )
    bash = service._build_bash_tool("t1")
    out = await bash.handler({"command": "echo hi"})

    assert out["is_error"] is True
    assert "unavailable" in out["content"][0]["text"]
