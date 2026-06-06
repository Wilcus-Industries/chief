"""TaskSession streaming, interrupt, and resume (ClaudeSDKClient faked)."""

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import RateLimitInfo

from chief.core.session import Final, Milestone, TaskSession


class FakeClient:
    """Structural stand-in for ClaudeSDKClient."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.connected = False
        self.queries: list[Any] = []
        self.interrupted = False
        self.model: str | None = None
        self.messages: list[Any] = []
        #: When set, ``connect`` raises it — a stand-in for a dead resume id (exit 1).
        self.fail_on_connect: Exception | None = None

    async def connect(self) -> None:
        if self.fail_on_connect is not None:
            raise self.fail_on_connect
        self.connected = True

    async def query(
        self, prompt: str | AsyncIterable[dict[str, Any]], session_id: str = "default"
    ) -> None:
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


def _result(cost: float | None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=cost,
    )


def _rate_limit(status: str) -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status),  # type: ignore[arg-type]
        uuid="rl-1",
        session_id="sess-1",
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


async def test_run_turn_captures_cost_from_result_message() -> None:
    client = FakeClient(ClaudeAgentOptions())
    client.messages = [
        _assistant(TextBlock(text="hi")),
        _result(cost=0.42),
    ]
    session = _session_with(client)

    [event async for event in session.run_turn("do it")]

    assert session.last_cost_usd == 0.42


async def test_cost_defaults_to_zero_when_result_has_none() -> None:
    client = FakeClient(ClaudeAgentOptions())
    client.messages = [_assistant(TextBlock(text="hi")), _result(cost=None)]
    session = _session_with(client)

    [event async for event in session.run_turn("do it")]

    assert session.last_cost_usd == 0.0


async def test_cost_resets_each_turn() -> None:
    client = FakeClient(ClaudeAgentOptions())
    session = _session_with(client)

    client.messages = [_result(cost=1.0)]
    [event async for event in session.run_turn("first")]
    assert session.last_cost_usd == 1.0

    # A turn with no ResultMessage must not carry the prior turn's cost forward.
    client.messages = [_assistant(TextBlock(text="hi"))]
    [event async for event in session.run_turn("second")]
    assert session.last_cost_usd == 0.0


async def test_run_turn_captures_rate_limit_status() -> None:
    client = FakeClient(ClaudeAgentOptions())
    client.messages = [_rate_limit("rejected"), _assistant(TextBlock(text="hi"))]
    session = _session_with(client)

    [event async for event in session.run_turn("do it")]

    assert session.last_rate_limit_status == "rejected"


async def test_rate_limit_status_starts_none() -> None:
    session = _session_with(FakeClient(ClaudeAgentOptions()))
    assert session.last_rate_limit_status is None


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


def test_fork_session_flag_flows_into_options() -> None:
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    session = TaskSession(
        model="claude-sonnet-4-6",
        resume="sess-casual",
        fork_session=True,
        client_factory=factory,
    )

    # /branch forks the casual session into a new thread: resume the casual id but fork
    # to a fresh one so the casual channel keeps compacting independently.
    assert captured["options"].fork_session is True
    assert captured["options"].resume == "sess-casual"
    assert session.session_id == "sess-casual"


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


def test_plugins_and_skills_flow_into_options() -> None:
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    TaskSession(
        model="claude-sonnet-4-6",
        plugins=[{"type": "local", "path": "vendor/chief-skills"}],
        skills=["docx", "claude-api"],
        client_factory=factory,
    )

    options = captured["options"]
    assert options.plugins == [{"type": "local", "path": "vendor/chief-skills"}]
    assert options.skills == ["docx", "claude-api"]


def test_plugins_and_skills_default_empty() -> None:
    # A plain session (every guest, and an owner with skills off) carries no plugin and
    # leaves skills unset, so the SDK discovers nothing — tier isolation by construction.
    captured: dict[str, ClaudeAgentOptions] = {}

    def factory(options: ClaudeAgentOptions) -> FakeClient:
        captured["options"] = options
        return FakeClient(options)

    TaskSession(model="claude-sonnet-4-6", client_factory=factory)

    assert captured["options"].plugins == []
    assert captured["options"].skills is None


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
