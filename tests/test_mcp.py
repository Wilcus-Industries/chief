"""MCP client: a real stdio server registers and dispatches end-to-end.

Servers are pure config now (no add_mcp_server tool) — the manager connects
whatever ``config.mcp_servers`` declares at boot.
"""

import sys
from pathlib import Path

from chief.agent.tools import ToolRegistry
from chief.mcpclient.manager import McpManager, ServerConfig
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
