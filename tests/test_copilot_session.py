"""CopilotBackend / CopilotTaskSession over the GitHub Copilot SDK (#76, part of #72).

The central mechanism is a *real* Copilot-SDK turn dispatched through the backend: the
event-model rewrite runs for real here — the callback→queue bridge, the
Milestone/Final mapping, turn-end detection, and cost/rate-limit capture — with only the
SDK client/session boundary faked, so a real turn drives without spawning the Copilot
runtime. This is the twin of ``tests/test_session.py`` (the claude-agent-sdk mapping).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from copilot.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    SessionErrorData,
    SessionEvent,
    SessionEventType,
    SessionIdleData,
    SessionLimitsExhaustedRequestedData,
    ToolExecutionStartData,
)

from chief.adapters.base import Attachment
from chief.core.backend import CopilotBackend
from chief.core.copilot_session import CopilotTaskSession, CopilotTurnError
from chief.core.session import Final, Milestone


def _event(data: Any) -> SessionEvent:
    """Wrap event data in a SessionEvent — the mapping inspects only ``.data``."""
    return SessionEvent(
        data=data,
        id=uuid4(),
        timestamp=datetime.now(UTC),
        type=SessionEventType.SESSION_INFO,
    )


def _msg(content: str) -> AssistantMessageData:
    return AssistantMessageData(content=content, message_id=uuid4().hex)


def _usage(cost: float | None) -> AssistantUsageData:
    return AssistantUsageData(model="auto", cost=cost)


class FakeCopilotSession:
    """Structural stand-in for ``copilot.CopilotSession`` — the faked SDK boundary.

    ``send`` replays the scripted event-data objects through the registered handler,
    exactly as the runtime would push them, ending with a ``SessionIdleData`` boundary.
    """

    def __init__(self, session_id: str, script: list[Any]) -> None:
        self.session_id = session_id
        self._script = script
        self._handler: Callable[[SessionEvent], None] | None = None
        self.sent: list[str] = []
        self.model_set: str | None = None
        self.aborted = False
        self.disconnected = False

    def on(self, handler: Callable[[SessionEvent], None]) -> Callable[[], None]:
        self._handler = handler
        return lambda: None

    async def send(self, prompt: str, *, attachments: Any = None) -> str:
        self.sent.append(prompt)
        assert self._handler is not None
        for data in self._script:
            self._handler(_event(data))
        return "msg-1"

    async def set_model(self, model: str) -> None:
        self.model_set = model

    async def abort(self) -> None:
        self.aborted = True

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeCopilotClient:
    """Structural stand-in for ``copilot.CopilotClient``."""

    def __init__(self, session: FakeCopilotSession) -> None:
        self._session = session
        self.started = False
        self.stopped = False
        self.create_kwargs: dict[str, Any] | None = None
        self.resume_args: tuple[str, dict[str, Any]] | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def create_session(self, **kwargs: Any) -> FakeCopilotSession:
        self.create_kwargs = kwargs
        return self._session

    async def resume_session(
        self, session_id: str, **kwargs: Any
    ) -> FakeCopilotSession:
        self.resume_args = (session_id, kwargs)
        return self._session


def _backend_with(
    script: list[Any], *, session_id: str = "sess-1"
) -> tuple[CopilotBackend, FakeCopilotClient, FakeCopilotSession]:
    session = FakeCopilotSession(session_id, script)
    client = FakeCopilotClient(session)
    backend = CopilotBackend(client_factory=lambda: client)
    return backend, client, session


async def test_owner_turn_returns_reply_through_backend() -> None:
    # AC1: with the copilot backend, an owner text message returns a model reply.
    backend, client, session = _backend_with(
        [_msg("hi from copilot"), SessionIdleData()]
    )
    task = backend.create_session(model="auto")

    events = [event async for event in task.run_turn("hello")]

    assert events == [Final(text="hi from copilot")]
    assert client.started is True
    assert session.sent == ["hello"]
    assert task.session_id == "sess-1"


async def test_events_map_to_milestones_and_final_in_order() -> None:
    # AC2: tool.execution_start → Milestone, assistant.message → Final, streamed in
    # arrival order; session.idle ends the turn (run_turn returns).
    backend, _client, _session = _backend_with(
        [
            ToolExecutionStartData(tool_call_id="c1", tool_name="shell", arguments={}),
            _msg("done"),
            SessionIdleData(),
        ]
    )
    task = backend.create_session(model="auto")

    events = [event async for event in task.run_turn("go")]

    assert events == [Milestone(text="using shell"), Final(text="done")]


async def test_multiple_assistant_messages_stream_as_separate_finals() -> None:
    # Per-block reply streaming (#64): each assistant message is its own Final.
    backend, _client, _session = _backend_with(
        [_msg("part one"), _msg("part two"), SessionIdleData()]
    )
    task = backend.create_session(model="auto")

    events = [event async for event in task.run_turn("go")]

    assert events == [Final(text="part one"), Final(text="part two")]


async def test_empty_response_is_no_reply() -> None:
    # A turn that streams no assistant text still yields exactly one Final(NO_REPLY).
    backend, _client, _session = _backend_with([SessionIdleData()])
    task = backend.create_session(model="auto")

    events = [event async for event in task.run_turn("hi")]

    assert events == [Final(text="(no reply)")]


async def test_cost_summed_from_usage_events() -> None:
    backend, _client, _session = _backend_with(
        [_usage(0.1), _msg("hi"), _usage(0.2), SessionIdleData()]
    )
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert task.last_cost_usd == pytest.approx(0.3)


async def test_cost_zero_on_quota_when_cost_none() -> None:
    # Copilot quota reports no dollar cost — usage.cost is None → last_cost_usd stays 0.
    backend, _client, _session = _backend_with(
        [_usage(None), _msg("hi"), SessionIdleData()]
    )
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert task.last_cost_usd == 0.0


async def test_cost_resets_each_turn() -> None:
    session = FakeCopilotSession("sess-1", [_usage(0.5), _msg("hi"), SessionIdleData()])
    client = FakeCopilotClient(session)
    task = CopilotTaskSession(model="auto", client_factory=lambda: client)

    [event async for event in task.run_turn("first")]
    assert task.last_cost_usd == pytest.approx(0.5)

    # A turn with no usage event must not carry the prior turn's cost forward.
    session._script = [_msg("hi again"), SessionIdleData()]
    [event async for event in task.run_turn("second")]
    assert task.last_cost_usd == 0.0


async def test_rate_limit_synthesised_on_limits_exhausted() -> None:
    # Copilot has no allowed/warning status; quota exhaustion synthesises "rejected".
    backend, _client, _session = _backend_with(
        [
            SessionLimitsExhaustedRequestedData(
                max_ai_credits=200, request_id="r1", used_ai_credits=200
            ),
            _msg("hi"),
            SessionIdleData(),
        ]
    )
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert task.last_rate_limit_status == "rejected"


async def test_rate_limit_status_starts_none() -> None:
    backend, _client, _session = _backend_with([SessionIdleData()])
    task = backend.create_session(model="auto")
    assert task.last_rate_limit_status is None


async def test_session_error_raises_turn_error() -> None:
    backend, _client, _session = _backend_with(
        [SessionErrorData(error_type="overloaded", message="try again later")]
    )
    task = backend.create_session(model="auto")

    with pytest.raises(CopilotTurnError, match="overloaded"):
        [event async for event in task.run_turn("go")]


async def test_resume_opens_resume_session() -> None:
    # A resume pointer routes through resume_session (not create_session).
    backend, client, _session = _backend_with(
        [_msg("resumed"), SessionIdleData()], session_id="sess-9"
    )
    task = backend.create_session(model="auto", resume="sess-9")

    events = [event async for event in task.run_turn("continue")]

    assert events == [Final(text="resumed")]
    assert client.resume_args is not None
    assert client.resume_args[0] == "sess-9"
    assert client.create_kwargs is None
    assert task.session_id == "sess-9"


async def test_interrupt_set_model_and_aclose_delegate() -> None:
    backend, client, session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]  # connect

    await task.interrupt()
    await task.set_model("gpt-5")
    await task.aclose()

    assert session.aborted is True
    assert session.model_set == "gpt-5"
    assert session.disconnected is True
    assert client.stopped is True


async def test_set_model_before_connect_uses_new_model_on_create() -> None:
    # set_model before the first turn stores the model; connect creates with it.
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="auto")

    await task.set_model("claude-sonnet-4.5")
    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["model"] == "claude-sonnet-4.5"


async def test_attachments_are_dropped_with_warning_this_slice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Text-only slice: attachments don't break the turn but are logged as dropped (#72).
    backend, _client, session = _backend_with([_msg("ok"), SessionIdleData()])
    task = backend.create_session(model="auto")
    att = Attachment(media_type="image/png", data=b"\x89PNG")

    with caplog.at_level(logging.WARNING):
        events = [event async for event in task.run_turn("what is this?", (att,))]

    assert events == [Final(text="ok")]
    assert session.sent == ["what is this?"]
    assert any("attachment" in r.message.lower() for r in caplog.records)
