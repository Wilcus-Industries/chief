"""TaskSession streaming, interrupt, and resume (ClaudeSDKClient faked)."""

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
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

    async def connect(self) -> None:
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
