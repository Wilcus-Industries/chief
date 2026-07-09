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
from copilot import ProviderConfig
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
from chief.config import Settings
from chief.core.backend import CopilotBackend
from chief.core.copilot_session import (
    CopilotTaskSession,
    CopilotTurnError,
    build_persona_system_message,
    find_vendor_identity_leak,
    openrouter_provider_config,
)
from chief.core.personas import build_system_prompt
from chief.core.session import Final, Milestone
from chief.memory.store import Fact
from chief.memory.versioning import NullVersioner


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


def _usage(cost: float | None, model: str = "auto") -> AssistantUsageData:
    return AssistantUsageData(model=model, cost=cost)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
        owner_telegram_id=42,
        telegram_bot_token="tg-secret",
        claude_code_oauth_token="oauth-secret",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


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


async def test_served_model_captured_from_usage_event() -> None:
    # #90: the actually-served model (event data.model) is readable off the session —
    # how the openrouter target class's live test asserts the requested model was used.
    backend, _client, _session = _backend_with(
        [_usage(0.1, model="anthropic/claude-haiku-4.5"), _msg("hi"), SessionIdleData()]
    )
    task = backend.create_session(model="anthropic/claude-haiku-4.5")
    assert isinstance(task, CopilotTaskSession)  # narrows past SessionProto's slice

    [event async for event in task.run_turn("go")]

    assert task.last_served_model == "anthropic/claude-haiku-4.5"


async def test_served_model_resets_each_turn() -> None:
    session = FakeCopilotSession(
        "sess-1", [_usage(0.1, model="gpt-5-mini"), _msg("hi"), SessionIdleData()]
    )
    client = FakeCopilotClient(session)
    task = CopilotTaskSession(model="auto", client_factory=lambda: client)

    [event async for event in task.run_turn("first")]
    assert task.last_served_model == "gpt-5-mini"

    session._script = [_msg("hi again"), SessionIdleData()]
    [event async for event in task.run_turn("second")]
    assert task.last_served_model is None


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


async def test_provider_forwarded_to_create_session() -> None:
    # #90: a BYOK provider config (e.g. openrouter_provider_config's result) reaches the
    # SDK's create_session call unchanged, alongside the requested model.
    provider = ProviderConfig(
        base_url="https://openrouter.ai/api/v1",
        api_key="or-key",
        type="openai",
    )
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="anthropic/claude-haiku-4.5", provider=provider)

    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["provider"] is provider
    assert client.create_kwargs["model"] == "anthropic/claude-haiku-4.5"


async def test_provider_forwarded_to_resume_session() -> None:
    provider = ProviderConfig(base_url="https://openrouter.ai/api/v1", api_key="or-key")
    backend, client, _session = _backend_with(
        [_msg("resumed"), SessionIdleData()], session_id="sess-9"
    )
    task = backend.create_session(
        model="auto", resume="sess-9", provider=provider
    )

    [event async for event in task.run_turn("continue")]

    assert client.resume_args is not None
    assert client.resume_args[1]["provider"] is provider


async def test_no_provider_defaults_to_none() -> None:
    # Plain Copilot quota (no BYOK override) — provider=None reaches the SDK call too.
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["provider"] is None


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


# --- Persona via section-customization (#78) --------------------------------------


async def test_persona_forwarded_as_customize_system_message() -> None:
    # #78 AC1: system_prompt threads through as a Copilot "customize" system message —
    # identity replaced with chief's persona, vendor voice removed, tool/safety/
    # environment scaffolding preserved (the documented non-replaceable sections).
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    persona = "I am chief, Will's personal AI assistant."

    task = backend.create_session(model="auto", system_prompt=persona)
    [event async for event in task.run_turn("hello")]

    assert client.create_kwargs is not None
    system_message = client.create_kwargs["system_message"]
    assert system_message == build_persona_system_message(persona)
    sections = system_message["sections"]
    assert sections["identity"] == {"action": "replace", "content": persona}
    assert sections["preamble"] == {"action": "remove"}
    assert sections["tone"] == {"action": "remove"}
    for preserved in (
        "tool_efficiency",
        "environment_context",
        "code_change_rules",
        "guidelines",
        "safety",
        "tool_instructions",
        "custom_instructions",
        "runtime_instructions",
        "last_instructions",
    ):
        assert sections[preserved] == {"action": "preserve"}


async def test_resume_forwards_system_message_too() -> None:
    # The persona override must apply on a resumed session, not just a fresh one.
    backend, client, _session = _backend_with(
        [_msg("resumed"), SessionIdleData()], session_id="sess-9"
    )
    persona = "I am chief."

    task = backend.create_session(model="auto", resume="sess-9", system_prompt=persona)
    [event async for event in task.run_turn("continue")]

    assert client.resume_args is not None
    _session_id, kwargs = client.resume_args
    assert kwargs["system_message"] == build_persona_system_message(persona)


async def test_no_persona_means_no_system_message_override() -> None:
    # Without a system_prompt, no customize config is built — the SDK's own default
    # prompt stands (mirrors ClaudeBackend forwarding system_prompt=None verbatim).
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["system_message"] is None


class _FakeMemory:
    """Minimal MemoryStore stand-in — mirrors ``tests/test_personas.py``'s fixture."""

    @property
    def versioner(self) -> NullVersioner:
        return NullVersioner()

    def facts_listing(self) -> str:
        return ""

    def soul(self) -> str:
        return "I am chief."

    def user(self) -> str:
        return "Will prefers concise replies."

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        raise NotImplementedError


def test_guest_persona_customize_config_carries_no_owner_content() -> None:
    # #78 AC1: tier-isolation holds through the new mapping — it is content-blind, so
    # whatever build_system_prompt already tier-scoped lands in `identity` verbatim,
    # and nothing owner-only leaks into a guest config.
    memory = _FakeMemory()
    owner_prompt = build_system_prompt(tier="owner", memory=memory, owner_name="Will")
    guest_prompt = build_system_prompt(tier="guest", memory=memory, owner_name="Will")

    owner_config = build_persona_system_message(owner_prompt)
    guest_config = build_persona_system_message(guest_prompt)

    assert owner_config is not None and owner_config["mode"] == "customize"
    assert guest_config is not None and guest_config["mode"] == "customize"
    owner_identity = owner_config["sections"]["identity"]["content"]
    guest_identity = guest_config["sections"]["identity"]["content"]
    assert "Will prefers concise replies." in owner_identity
    assert "Will prefers concise replies." not in guest_identity
    assert "receptionist" in guest_identity


# --- Vendor-identity leakage check (#78) -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Hi, I'm GitHub Copilot, here to help with your code.",
        "As Copilot CLI, I can run shell commands for you.",
        "I am Copilot, an AI created by GitHub to assist developers.",
        "I'm Copilot, a coding assistant built by GitHub.",
    ],
)
def test_leak_detector_flags_vendor_identity_phrases(text: str) -> None:
    assert find_vendor_identity_leak(text) is not None


def test_leak_detector_passes_clean_chief_reply() -> None:
    reply = "Hey, pushed the fix and the tests are green. Anything else?"
    assert find_vendor_identity_leak(reply) is None


def test_leak_detector_does_not_false_positive_on_bare_mention() -> None:
    # Chief mentioning the product by name in passing isn't self-identification.
    reply = "I switched your backend from Copilot to Claude last week, as you asked."
    assert find_vendor_identity_leak(reply) is None


async def test_persona_turn_reply_has_no_vendor_identity_leak() -> None:
    # #78 AC1's central-mechanism assertion: a full turn is dispatched through the
    # backend with chief's persona installed as the customize-mode system message, and
    # the reply is scanned for vendor self-identification. The SDK client/session
    # boundary is faked (per this module's docstring); the mapping, forwarding, and
    # scan all run for real — only the scripted reply text stands in for the live
    # model's actual output (see tests/test_copilot_backend_live.py for the live
    # opt-in probe of real-model behavior).
    persona = "I am chief, your personal AI assistant. Speak plainly, be concise."
    backend, client, _session = _backend_with(
        [_msg("Done, pushed the fix, tests are green."), SessionIdleData()]
    )

    task = backend.create_session(model="auto", system_prompt=persona)
    events = [event async for event in task.run_turn("ship it")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["system_message"]["sections"]["identity"] == {
        "action": "replace",
        "content": persona,
    }
    finals = [e.text for e in events if isinstance(e, Final)]
    assert finals
    assert all(find_vendor_identity_leak(text) is None for text in finals)


async def test_leak_detector_would_catch_an_unconfigured_vendor_reply() -> None:
    # Negative control, so the assertion above isn't vacuous: if the persona override
    # silently failed to take and Copilot answered as itself, the same check fails.
    backend, _client, _session = _backend_with(
        [
            _msg("I'm GitHub Copilot, how can I help with your code?"),
            SessionIdleData(),
        ]
    )
    task = backend.create_session(model="auto")

    events = [event async for event in task.run_turn("hi")]

    finals = [e.text for e in events if isinstance(e, Final)]
    assert any(find_vendor_identity_leak(text) is not None for text in finals)


# --- Provider target classes / OpenRouter BYOK (#90) -------------------------------


def test_openrouter_provider_config_shape() -> None:
    # #90: type openai, OpenRouter's endpoint, key sourced from settings.
    settings = _settings(openrouter_api_key="or-secret-key")

    provider = openrouter_provider_config(settings)

    assert provider["type"] == "openai"
    assert provider["base_url"] == "https://openrouter.ai/api/v1"
    assert provider["api_key"] == "or-secret-key"


def test_openrouter_provider_config_key_is_not_hardcoded() -> None:
    # The key tracks whatever settings.openrouter_api_key holds, never a fixed literal.
    settings_a = _settings(openrouter_api_key="key-a")
    settings_b = _settings(openrouter_api_key="key-b")

    assert openrouter_provider_config(settings_a)["api_key"] == "key-a"
    assert openrouter_provider_config(settings_b)["api_key"] == "key-b"


def test_openrouter_provider_config_omits_key_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # total=False: an unset key stays absent rather than masked as an empty string, so
    # a misconfigured deploy surfaces as an OpenRouter auth error, not a silent no-op.
    # Hermetic: an ambient OPENROUTER_API_KEY on the host must not leak in here — the
    # env source would otherwise fill the field pydantic-settings sees as "unset".
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = _settings()

    provider = openrouter_provider_config(settings)

    assert "api_key" not in provider
