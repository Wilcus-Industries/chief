"""MCP client: a real stdio server registers and dispatches end-to-end.

Servers are pure config now (no add_mcp_server tool) — the manager connects
whatever ``config.mcp_servers`` declares at boot.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from chief.mcpclient.manager import McpManager, ServerConfig
from chief.provider.base import ToolCall
from chief.tools import ToolRegistry

SERVER_SCRIPT = """
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("testsrv")


@mcp.tool()
def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b


@mcp.tool()
def env_var(name: str) -> str:
    \"\"\"Return the named environment variable as seen by this process.\"\"\"
    return os.environ.get(name, "")


@mcp.tool()
def cwd() -> str:
    \"\"\"Return this process's current working directory.\"\"\"
    return os.getcwd()


mcp.run()
"""


def write_server(tmp_path: Path) -> tuple[str, ...]:
    script = tmp_path / "server.py"
    script.write_text(SERVER_SCRIPT)
    return (sys.executable, str(script))


# Sleeps before it even builds its MCP app, so the client's handshake blocks
# for the full delay — a real slow first launch, not a patched clock.
SLOW_SERVER_SCRIPT = """
import time

from mcp.server.fastmcp import FastMCP

time.sleep(1.0)

mcp = FastMCP("slowsrv")


@mcp.tool()
def ping() -> str:
    return "pong"


mcp.run()
"""


def write_slow_server(tmp_path: Path) -> tuple[str, ...]:
    script = tmp_path / "slow_server.py"
    script.write_text(SLOW_SERVER_SCRIPT)
    return (sys.executable, str(script))


async def test_stdio_server_tools_register_and_dispatch(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        count = await manager.connect(
            ServerConfig(name="testsrv", command=write_server(tmp_path))
        )
        assert count == 3
        assert any(s.name == "mcp_testsrv_add" for s in registry.specs())
        result = await registry.dispatch(
            ToolCall(id="1", name="mcp_testsrv_add", arguments={"a": 1, "b": 2})
        )
        assert result == "3"
    finally:
        await manager.stop()


async def test_declared_env_var_is_visible_in_child_process(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        await manager.connect(
            ServerConfig(
                name="testsrv",
                command=write_server(tmp_path),
                env={"MCP_TEST_SECRET": "s3cr3t"},
            )
        )
        result = await registry.dispatch(
            ToolCall(
                id="1",
                name="mcp_testsrv_env_var",
                arguments={"name": "MCP_TEST_SECRET"},
            )
        )
        assert result == "s3cr3t"
    finally:
        await manager.stop()


async def test_unrelated_daemon_env_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TEST_UNRELATED", "should-not-leak")
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        await manager.connect(
            ServerConfig(
                name="testsrv",
                command=write_server(tmp_path),
                env={"MCP_TEST_SECRET": "s3cr3t"},
            )
        )
        result = await registry.dispatch(
            ToolCall(
                id="1",
                name="mcp_testsrv_env_var",
                arguments={"name": "MCP_TEST_UNRELATED"},
            )
        )
        assert result == ""
    finally:
        await manager.stop()


async def test_declared_cwd_is_honoured(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        await manager.connect(
            ServerConfig(
                name="testsrv",
                command=write_server(tmp_path),
                cwd=str(workdir),
            )
        )
        result = await registry.dispatch(
            ToolCall(id="1", name="mcp_testsrv_cwd", arguments={})
        )
        assert result == str(workdir)
    finally:
        await manager.stop()


async def test_no_env_or_cwd_behaves_as_before(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        count = await manager.connect(
            ServerConfig(name="testsrv", command=write_server(tmp_path))
        )
        assert count == 3
    finally:
        await manager.stop()


def test_server_declaring_no_timeout_gets_30_seconds() -> None:
    assert ServerConfig(name="testsrv", command=("noop",)).timeout == 30.0


async def test_slow_real_server_exceeding_timeout_fails_loudly_and_cancels_task(
    tmp_path: Path,
) -> None:
    """A real, genuinely slow subprocess (not a patched clock) blows a short
    configured timeout: the connect fails loudly naming the server, its
    supervising task is gone (asserted against the live event loop, not
    inferred from the manager's own bookkeeping), and the manager still
    works for other servers afterward."""
    registry = ToolRegistry()
    manager = McpManager(registry)
    other_tasks_before = {
        t for t in asyncio.all_tasks() if t is not asyncio.current_task()
    }
    try:
        with pytest.raises(TimeoutError, match="slowsrv"):
            await manager.connect(
                ServerConfig(
                    name="slowsrv",
                    command=write_slow_server(tmp_path),
                    timeout=0.3,
                )
            )
        leaked = {
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        } - other_tasks_before
        assert leaked == set()

        # The rest of the daemon boots fine: a second, healthy server still
        # connects and registers its tools on the very same manager.
        count = await manager.connect(
            ServerConfig(name="testsrv", command=write_server(tmp_path))
        )
        assert count == 3
        assert any(s.name == "mcp_testsrv_add" for s in registry.specs())
    finally:
        await manager.stop()


async def test_timeout_longer_than_default_connects_once_ready(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    try:
        count = await manager.connect(
            ServerConfig(
                name="slowsrv",
                command=write_slow_server(tmp_path),
                timeout=35.0,
            )
        )
        assert count == 1
        assert any(s.name == "mcp_slowsrv_ping" for s in registry.specs())
    finally:
        await manager.stop()
