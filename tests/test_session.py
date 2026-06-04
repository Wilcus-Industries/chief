"""TaskSession streaming, interrupt, and resume (ClaudeSDKClient faked)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    TextBlock,
    ToolUseBlock,
)

from chief.core.session import Final, Milestone, TaskSession


class FakeClient:
    """Structural stand-in for ClaudeSDKClient."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.connected = False
        self.queries: list[str] = []
        self.interrupted = False
        self.model: str | None = None
        self.messages: list[Any] = []
        #: When set, ``connect`` raises it — a stand-in for a dead resume id (exit 1).
        self.fail_on_connect: Exception | None = None

    async def connect(self) -> None:
        if self.fail_on_connect is not None:
            raise self.fail_on_connect
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True

    async def set_model(self, model: str | None = None) -> None:
        self.model = model

    async def disconnect(self) -> None:
        self.connected = False


def _session_with(client: FakeClient) -> TaskSession:
    return TaskSession(model="claude-sonnet-4-6", client_factory=lambda _opts: client)


def _assistant(*blocks: Any, session_id: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=list(blocks), model="claude-sonnet-4-6", session_id=session_id
    )


async def test_run_turn_streams_milestones_then_final() -> None:
    client = FakeClient(ClaudeAgentOptions())
    client.messages = [
        _assistant(ToolUseBlock(id="t1", name="Bash", input={})),
        _assistant(TextBlock(text="hello "), TextBlock(text="world")),
        _assistant(TextBlock(text="!"), session_id="sess-1"),
    ]
    session = _session_with(client)

    events = [event async for event in session.run_turn("do it")]

    assert events == [Milestone(text="using Bash"), Final(text="hello world!")]
    assert client.connected is True
    assert client.queries == ["do it"]
    assert session.session_id == "sess-1"


async def test_run_turn_empty_response_is_no_reply() -> None:
    client = FakeClient(ClaudeAgentOptions())
    client.messages = []
    session = _session_with(client)

    events = [event async for event in session.run_turn("hi")]

    assert events == [Final(text="(no reply)")]


async def test_interrupt_and_aclose_delegate() -> None:
    client = FakeClient(ClaudeAgentOptions())
    session = _session_with(client)
    await session._ensure_connected()

    await session.interrupt()
    await session.aclose()

    assert client.interrupted is True
    assert client.connected is False


def test_resume_passes_session_id_into_options() -> None:
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    session = TaskSession(
        model="claude-sonnet-4-6", resume="sess-prior", client_factory=factory
    )

    assert captured["options"].resume == "sess-prior"
    assert session.session_id == "sess-prior"


def test_memory_params_flow_into_options() -> None:
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    TaskSession(
        model="claude-sonnet-4-6",
        system_prompt="# Soul\nI am chief.",
        cwd="/memory",
        allowed_tools=["Read", "Glob", "Grep"],
        client_factory=factory,
    )

    options = captured["options"]
    assert options.system_prompt == "# Soul\nI am chief."
    assert options.cwd == "/memory"
    assert options.allowed_tools == ["Read", "Glob", "Grep"]


def test_mcp_and_disallowed_tools_flow_into_options() -> None:
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    TaskSession(
        model="claude-sonnet-4-6",
        mcp_servers={"gcal": {"type": "http", "url": "http://mcp-gcal:3000/"}},
        disallowed_tools=["mcp__gcal__delete-event"],
        client_factory=factory,
    )

    options = captured["options"]
    assert options.mcp_servers == {
        "gcal": {"type": "http", "url": "http://mcp-gcal:3000/"}
    }
    assert options.disallowed_tools == ["mcp__gcal__delete-event"]


async def test_dead_resume_falls_back_to_fresh_session() -> None:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options)
        if options.resume is not None:
            # A dead resume id makes the CLI exit 1 on the turn's first message.
            client.fail_on_connect = ProcessError("session not found", exit_code=1)
        else:
            client.messages = [
                _assistant(TextBlock(text="fresh"), session_id="sess-new")
            ]
        clients.append(client)
        return client

    session = TaskSession(
        model="claude-sonnet-4-6", resume="dead-sess", client_factory=factory
    )

    events = [event async for event in session.run_turn("hi")]

    assert events == [Final(text="fresh")]
    assert session.session_id == "sess-new"
    assert len(clients) == 2  # rebuilt once after the dead resume
    assert clients[1].options.resume is None


class RaisingClient(FakeClient):
    """Yields its messages, then raises ``raise_after`` from ``receive_response``."""

    raise_after: Exception

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message
        raise self.raise_after


async def test_process_error_after_partial_stream_propagates() -> None:
    clients: list[FakeClient] = []

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = RaisingClient(options)
        client.messages = [_assistant(ToolUseBlock(id="t1", name="Bash", input={}))]
        client.raise_after = ProcessError("died mid-turn", exit_code=1)
        clients.append(client)
        return client

    session = TaskSession(
        model="claude-sonnet-4-6", resume="sess-prior", client_factory=factory
    )

    events: list[Any] = []
    with pytest.raises(ProcessError):
        async for event in session.run_turn("hi"):
            events.append(event)

    # Milestone streamed before the error — no fresh-session replay of a partial turn.
    assert events == [Milestone(text="using Bash")]
    assert len(clients) == 1


async def test_process_error_without_resume_propagates() -> None:
    def factory(options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options)
        client.fail_on_connect = ProcessError("boom", exit_code=1)
        return client

    session = TaskSession(model="claude-sonnet-4-6", client_factory=factory)

    with pytest.raises(ProcessError):
        [event async for event in session.run_turn("hi")]
