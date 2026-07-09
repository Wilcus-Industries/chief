"""The agent backend seam (#88): CopilotBackend adapts chief's gate onto the SDK.

The claude-agent-sdk backend and the ``select_backend`` registry are gone — the Copilot
SDK is chief's sole harness. The central mechanism here is a *real* backend turn: a fake
Copilot client/session boundary (the third-party subprocess) records the kwargs
``create_session`` forwards, so the backend's job — turning chief's SDK-agnostic
``can_use_tool`` into the Copilot SDK's ``on_permission_request`` — is exercised for
real. The event-mapping half is covered by ``tests/test_copilot_session.py`` and the
hook adaptation by ``tests/test_copilot_gate.py``.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext
from copilot.session_events import SessionEvent, SessionEventType, SessionIdleData

from chief.core.backend import CopilotBackend
from chief.core.copilot_session import CopilotClientFactory
from chief.core.session import Final


class FakeSession:
    """Structural stand-in for a Copilot session — replays a single idle event."""

    session_id = "sess-1"

    def __init__(self) -> None:
        self._handler: Callable[[SessionEvent], None] | None = None

    def on(self, handler: Callable[[SessionEvent], None]) -> Callable[[], None]:
        self._handler = handler
        return lambda: None

    async def send(self, prompt: str, *, attachments: Any = None) -> str:
        assert self._handler is not None
        self._handler(
            SessionEvent(
                data=SessionIdleData(),
                id=uuid4(),
                timestamp=datetime.now(UTC),
                type=SessionEventType.SESSION_INFO,
            )
        )
        return "msg-1"

    async def set_model(self, model: str) -> None: ...
    async def abort(self) -> None: ...
    async def disconnect(self) -> None: ...


class FakeClient:
    """Records the kwargs ``create_session`` is called with (the faked boundary)."""

    def __init__(self) -> None:
        self.create_kwargs: dict[str, Any] | None = None
        self._session = FakeSession()

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def create_session(self, **kwargs: Any) -> FakeSession:
        self.create_kwargs = kwargs
        return self._session


def _factory(client: FakeClient) -> CopilotClientFactory:
    return cast(CopilotClientFactory, lambda: client)


async def _allow(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow:
    return PermissionResultAllow()


async def test_backend_runs_a_real_turn_through_the_copilot_session() -> None:
    # create_session builds a CopilotTaskSession whose run_turn streams the SDK events
    # as chief Final events; only the client/session subprocess is faked.
    client = FakeClient()
    session = CopilotBackend(client_factory=_factory(client)).create_session(
        model="auto"
    )

    events = [event async for event in session.run_turn("hi")]

    assert events == [Final(text="(no reply)")]
    assert client.create_kwargs is not None
    assert client.create_kwargs["model"] == "auto"


async def test_backend_adapts_can_use_tool_onto_the_copilot_handler() -> None:
    # The backend's job: chief's SDK-agnostic can_use_tool becomes the Copilot SDK's
    # on_permission_request handler, threaded into create_session.
    client = FakeClient()
    session = CopilotBackend(client_factory=_factory(client)).create_session(
        model="auto", can_use_tool=_allow
    )

    [event async for event in session.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["on_permission_request"] is not None


async def test_backend_forwards_no_gate_when_unwired() -> None:
    # No can_use_tool / hooks → the SDK gets None for both, not an empty handler.
    client = FakeClient()
    session = CopilotBackend(client_factory=_factory(client)).create_session(
        model="auto"
    )

    [event async for event in session.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["on_permission_request"] is None
    assert client.create_kwargs["hooks"] is None
