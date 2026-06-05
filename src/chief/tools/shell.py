"""The owner's ``bash`` tool: an in-process MCP tool forwarding to the sandbox (M7).

The shell **cannot** be the SDK's built-in ``Bash`` tool: that runs inside ``core``,
where ``CLAUDE_CODE_OAUTH_TOKEN`` lives in the process environment, so a shell child
could ``env | grep CLAUDE`` and leak the Max token. Instead the owner gets an in-process
SDK MCP tool (``mcp__chief_shell__bash``) that forwards each command over a socket to
the secret-free sandbox container's shell (:mod:`chief.sandbox.shell_server`).

:class:`ShellService` mirrors :class:`~chief.tools.google.GoogleService`: ``tasks.py``
wires it uniformly. One difference — the bash tool is built **per task** with the task's
key baked into its closure, because an in-process MCP handler receives only the tool
input (no caller context), and each task must address its **own** persistent shell.
Keeping the tool out of ``allowed_tools`` routes every command through ``can_use_tool``
→ the owner's approval card (default-ask), exactly like a Google write.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

from ..sandbox.shell_server import ENCODING

#: The SDK names an in-process MCP tool ``mcp__<server>__<tool>``.
SERVER_NAME = "chief_shell"
TOOL_NAME = f"mcp__{SERVER_NAME}__bash"

#: The sandbox shell's two invariants, single-sourced here so the tool description and
#: the persona guidance (:mod:`chief.core.personas`) can't drift: a secret-free
#: environment, and chaining steps in one command rather than across separate calls.
SANDBOX_SHELL_CONTRACT = (
    "There are no secrets in this environment. Chain steps with && in one command "
    "rather than relying on separate calls."
)

_BASH_DESCRIPTION = (
    "Run a bash command in a sandboxed Linux container. The working directory is "
    "/workspace (a scratch space shared with your Read/Write/Edit tools) and has "
    "internet access. Shell state — environment variables, the current directory, "
    "background jobs — persists across calls within this task (but not across a "
    f"restart). {SANDBOX_SHELL_CONTRACT} Commands require approval unless the owner "
    "has pre-approved them."
)

#: Grace added to the server's own per-command timeout so the sandbox returns its
#: timeout result (exit 124) before the client gives up on the read.
_CLIENT_GRACE_SECONDS = 30.0


async def run_command(
    host: str,
    port: int,
    session_id: str,
    command: str,
    *,
    read_timeout: float,
) -> dict[str, Any]:
    """Send one command to the sandbox shell server and return its JSON result.

    Opens a short-lived connection per command; the sandbox keeps the *shell* alive
    keyed by ``session_id``, so cwd/env persist regardless of the connection lifetime.
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request = json.dumps({"session_id": session_id, "command": command})
        writer.write((request + "\n").encode(ENCODING))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=read_timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    if not line:
        raise ConnectionError("sandbox closed the connection without a response")
    result: dict[str, Any] = json.loads(line)
    return result


def format_result(result: dict[str, Any]) -> dict[str, Any]:
    """Render the sandbox's result dict as an MCP tool result (text + is_error)."""
    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    exit_code = int(result.get("exit_code", 0))
    truncated = bool(result.get("truncated", False))
    segments: list[str] = []
    if stdout:
        segments.append(stdout)
    if stderr:
        segments.append(f"[stderr]\n{stderr}")
    if exit_code != 0:
        segments.append(f"[exit code {exit_code}]")
    if truncated:
        segments.append("[output truncated]")
    text = "\n".join(segments) if segments else "(no output)"
    return {"content": [{"type": "text", "text": text}], "is_error": exit_code != 0}


@dataclass(frozen=True)
class ShellService:
    """Sandbox socket coordinates + limits; builds the per-task in-process bash tool."""

    host: str
    port: int
    timeout_seconds: float
    output_limit: int
    server_name: str = SERVER_NAME

    @property
    def tool_name(self) -> str:
        """The SDK-qualified ``mcp__chief_shell__bash`` name (gate/policy wiring)."""
        return f"mcp__{self.server_name}__bash"

    @property
    def read_timeout(self) -> float:
        """Per-command client read budget: the sandbox's own timeout plus grace.

        Shared by the bash tool closure and the scheduler's bash fires
        (:mod:`chief.core.scheduler`) so the grace is computed in exactly one place.
        """
        return self.timeout_seconds + _CLIENT_GRACE_SECONDS

    def _build_bash_tool(self, session_key: str) -> SdkMcpTool[Any]:
        host, port = self.host, self.port
        read_timeout = self.read_timeout

        @tool("bash", _BASH_DESCRIPTION, {"command": str})
        async def bash(args: dict[str, Any]) -> dict[str, Any]:
            command = str(args.get("command", ""))
            try:
                result = await run_command(
                    host, port, session_key, command, read_timeout=read_timeout
                )
            except Exception as exc:  # sandbox down/unreachable — surface, don't crash
                return {
                    "content": [
                        {"type": "text", "text": f"shell unavailable: {exc}"}
                    ],
                    "is_error": True,
                }
            return format_result(result)

        return bash

    def server_config(self, *, session_key: str) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for this task's bash tool."""
        return create_sdk_mcp_server(
            self.server_name, tools=[self._build_bash_tool(session_key)]
        )
