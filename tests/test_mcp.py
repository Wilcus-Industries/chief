"""MCP client: a real stdio server registers and dispatches end-to-end.

Servers are pure config now (no add_mcp_server tool) — the manager connects
whatever ``config.mcp_servers`` declares at boot.
"""

import sys
from pathlib import Path

import pytest

from chief.agent.tools import ToolRegistry
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.provider.base import ToolCall

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
