"""MCP client: a real stdio server end-to-end, plus self-added persistence."""

import sys
from pathlib import Path

from chief.agent.tools import ToolRegistry
from chief.audit import AuditLog
from chief.mcpclient.manager import McpManager, ServerConfig
from chief.mcpclient.tools import load_self_added, register_mcp_tools
from chief.provider.base import ToolCall

SERVER_SCRIPT = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("testsrv")


@mcp.tool()
def add(a: int, b: int) -> int:
    \"\"\"Add two numbers.\"\"\"
    return a + b


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
        assert count == 1
        assert any(s.name == "mcp_testsrv_add" for s in registry.specs())
        result = await registry.dispatch(
            ToolCall(id="1", name="mcp_testsrv_add", arguments={"a": 1, "b": 2})
        )
        assert result == "3"
    finally:
        await manager.stop()


async def test_add_mcp_server_tool_validates_and_persists(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = McpManager(registry)
    store_path = tmp_path / "mcp_servers.json"
    register_mcp_tools(
        registry, manager, AuditLog(tmp_path / "audit.jsonl"), path=store_path
    )
    both = await registry.dispatch(
        ToolCall(
            id="1",
            name="add_mcp_server",
            arguments={"name": "x", "rationale": "r", "url": "u", "command": ["c"]},
        )
    )
    assert both.startswith("error: give exactly one")
    try:
        added = await registry.dispatch(
            ToolCall(
                id="2",
                name="add_mcp_server",
                arguments={
                    "name": "testsrv",
                    "rationale": "need adding",
                    "command": list(write_server(tmp_path)),
                },
            )
        )
        assert added == "mcp server 'testsrv' connected with 1 tools"
    finally:
        await manager.stop()
    loaded = load_self_added(store_path)
    assert len(loaded) == 1
    assert loaded[0].name == "testsrv"
    assert loaded[0].command is not None
