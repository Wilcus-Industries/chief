"""AgentBackend seam (issue #75): ClaudeBackend wraps claude-agent-sdk; config selects.

The central mechanism is a *real* claude-agent-sdk turn dispatched through the backend:
the SDK options are built for real and a real :class:`TaskSession` drives the turn —
only the SDK subprocess client (the third-party boundary) is faked. Behaviour is
unchanged from driving ``TaskSession`` directly; the seam is where a later Copilot
backend (#72) will implement the same contract against a different SDK.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    TextBlock,
    ToolPermissionContext,
)
from claude_agent_sdk.types import HookEvent

from chief.core.backend import ClaudeBackend, CopilotBackend, select_backend
from chief.core.session import Final


class FakeClient:
    """Structural stand-in for ClaudeSDKClient — the faked SDK boundary."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.connected = False
        self.queries: list[Any] = []

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(
            content=[TextBlock(text="hi")], model="m", session_id="sess-1"
        )

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str | None = None) -> None: ...

    async def disconnect(self) -> None:
        self.connected = False


async def test_claude_backend_runs_real_sdk_turn_through_seam() -> None:
    # A real claude-agent-sdk turn flows through the backend: create_session builds a
    # TaskSession whose run_turn streams the SDK messages as Final events. Only the
    # subprocess client is faked.
    captured: dict[str, FakeClient] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options)
        captured["client"] = client
        return client

    backend = ClaudeBackend(client_factory=factory)
    session = backend.create_session(model="claude-sonnet-4-6")

    events = [event async for event in session.run_turn("do it")]

    assert events == [Final(text="hi")]
    assert captured["client"].connected is True
    assert captured["client"].queries == ["do it"]
    assert session.session_id == "sess-1"


async def test_claude_backend_wires_permission_hook_tools_and_resume() -> None:
    # The seam forwards the permission callback + pre-tool hook, the in-process tools
    # and MCP servers, and the resume pointer into the SDK options verbatim.
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    async def _allow(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow:
        return PermissionResultAllow()

    can_use: CanUseTool = _allow
    hooks: dict[HookEvent, list[HookMatcher]] = {"PreToolUse": []}
    mcp_servers: dict[str, Any] = {"chief_shell": {"type": "sse", "url": "http://x"}}

    ClaudeBackend(client_factory=factory).create_session(
        model="m",
        resume="sess-9",
        can_use_tool=can_use,
        hooks=hooks,
        allowed_tools=["Read"],
        disallowed_tools=["Bash"],
        mcp_servers=mcp_servers,
    )

    options = captured["options"]
    assert options.resume == "sess-9"
    assert options.can_use_tool is can_use
    assert options.hooks == hooks
    assert options.allowed_tools == ["Read"]
    assert options.disallowed_tools == ["Bash"]
    assert options.mcp_servers == mcp_servers


def test_select_backend_claude_returns_claude_backend() -> None:
    assert isinstance(select_backend("claude"), ClaudeBackend)


def test_select_backend_copilot_returns_copilot_backend() -> None:
    # The Copilot backend (#76) is now registered alongside claude.
    assert isinstance(select_backend("copilot"), CopilotBackend)


def test_select_backend_rejects_unknown() -> None:
    # "claude" and "copilot" are valid; an unknown name is a config error, not a
    # silent fallback.
    with pytest.raises(ValueError, match="unknown agent_backend"):
        select_backend("gemini")
