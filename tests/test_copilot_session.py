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
from typing import Any, cast
from uuid import uuid4

import pytest
from copilot import ProviderConfig
from copilot._jsonrpc import JsonRpcError, ProcessExitedError
from copilot.generated.rpc import SessionsForkRequest, SessionsForkResult
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
    read_premium_requests,
)
from chief.core.personas import build_system_prompt
from chief.core.session import Final, Milestone
from chief.memory.store import Fact
from chief.memory.versioning import NullVersioner
from chief.tools.guest import GuestService


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


class _FakeSessionsRpc:
    """Fake ``client.rpc.sessions`` — records the fork source, returns a new id."""

    def __init__(self, client: "FakeCopilotClient") -> None:
        self._client = client

    async def fork(
        self, params: SessionsForkRequest, *, timeout: float | None = None
    ) -> SessionsForkResult:
        self._client.forked_from = params.session_id
        return SessionsForkResult(session_id=self._client.forked_session_id)


class _FakeServerRpc:
    """Fake ``client.rpc`` — only its ``sessions`` group is reached (issue #93)."""

    def __init__(self, client: "FakeCopilotClient") -> None:
        self.sessions = _FakeSessionsRpc(client)


class FakeCopilotClient:
    """Structural stand-in for ``copilot.CopilotClient``."""

    def __init__(
        self, session: FakeCopilotSession, *, forked_session_id: str = "sess-forked"
    ) -> None:
        self._session = session
        self.started = False
        self.stopped = False
        self.create_kwargs: dict[str, Any] | None = None
        self.resume_args: tuple[str, dict[str, Any]] | None = None
        #: Set by ``rpc.sessions.fork`` to the source id it was asked to fork (#93);
        #: stays ``None`` when no fork happened, so tests can assert fork was skipped.
        self.forked_from: str | None = None
        #: The id ``rpc.sessions.fork`` returns for the new, independent session.
        self.forked_session_id = forked_session_id

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    @property
    def rpc(self) -> _FakeServerRpc:
        return _FakeServerRpc(self)

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


# --- Branch forks the casual session, never shares it (#93) --------------------------


async def test_branch_forks_source_session_into_independent_id() -> None:
    # AC1/AC2 (#93): /branch under CopilotBackend must FORK the casual channel's
    # session, not merely resume it — so the branched thread gets an independent copy
    # whose id differs from the casual's, and turns in one never mutate the other's
    # context. This is exactly TaskManager.branch's request:
    # create_session(resume=<casual id>, fork_session=True).
    session = FakeCopilotSession("sess-forked", [_msg("branched"), SessionIdleData()])
    client = FakeCopilotClient(session, forked_session_id="sess-forked")
    backend = CopilotBackend(client_factory=lambda: client)

    task = backend.create_session(
        model="auto", resume="sess-casual", fork_session=True
    )
    events = [event async for event in task.run_turn("carry on")]

    assert events == [Final(text="branched")]
    # The fork sourced from the casual channel's id — the SDK leaves that id untouched.
    assert client.forked_from == "sess-casual"
    # The live turn drove the FORKED session, not the casual one: we resumed the new id,
    # so the branched thread's persisted id is distinct from the casual's.
    assert client.resume_args is not None
    assert client.resume_args[0] == "sess-forked"
    assert task.session_id == "sess-forked"
    assert task.session_id != "sess-casual"


async def test_resume_without_fork_does_not_fork() -> None:
    # A plain resume (fork_session False) must NOT fork — it resumes the id directly, so
    # an ordinary reopen keeps its own session as before. #93 scopes the fork strictly
    # to the branch path.
    backend, client, _session = _backend_with(
        [_msg("resumed"), SessionIdleData()], session_id="sess-9"
    )
    task = backend.create_session(model="auto", resume="sess-9")
    [event async for event in task.run_turn("continue")]

    assert client.forked_from is None  # fork was never invoked
    assert client.resume_args is not None
    assert client.resume_args[0] == "sess-9"  # resumed the id directly, unforked
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


# --- Tools + MCP servers forwarded to the SDK (#80) --------------------------------


def _guest_service() -> tuple[GuestService, list[str]]:
    relayed: list[str] = []

    async def relay(text: str) -> None:
        relayed.append(text)

    return GuestService(relay=relay, from_label="Dana"), relayed


async def test_in_process_and_http_servers_reach_create_session() -> None:
    # AC1: chief's mixed mcp_servers → the SDK's two inputs. The in-process guest server
    # becomes a flat SDK-qualified custom tool; the Google/browser HTTP config passes
    # through as an mcp_server; disallowed_tools becomes excluded_tools.
    service, _relayed = _guest_service()
    http = {"type": "http", "url": "http://mcp-calendar:8000"}
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(
        model="auto",
        mcp_servers={
            service.server_name: service.server_config(),
            "chief_calendar": http,
        },
        disallowed_tools=["Bash", "BashOutput"],
    )

    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    tool_names = [t.name for t in client.create_kwargs["tools"]]
    assert tool_names == ["mcp__chief_guest__leave_message"]
    assert client.create_kwargs["mcp_servers"] == {"chief_calendar": http}
    assert client.create_kwargs["excluded_tools"] == ["Bash", "BashOutput"]


async def test_tools_reach_resume_session_too() -> None:
    # The tool surface is non-persisted, so a resumed session must be re-supplied it.
    service, _relayed = _guest_service()
    backend, client, _session = _backend_with(
        [_msg("resumed"), SessionIdleData()], session_id="sess-9"
    )
    task = backend.create_session(
        model="auto",
        resume="sess-9",
        mcp_servers={service.server_name: service.server_config()},
    )

    [event async for event in task.run_turn("continue")]

    assert client.resume_args is not None
    _sid, kwargs = client.resume_args
    assert [t.name for t in kwargs["tools"]] == ["mcp__chief_guest__leave_message"]


async def test_no_tools_forwards_none_not_empty() -> None:
    # No mcp_servers/disallowed → the SDK sees None (its own defaults), not [] which
    # (for excluded_tools) would read as an explicit empty filter.
    backend, client, _session = _backend_with([_msg("hi"), SessionIdleData()])
    task = backend.create_session(model="auto")

    [event async for event in task.run_turn("go")]

    assert client.create_kwargs is not None
    assert client.create_kwargs["tools"] is None
    assert client.create_kwargs["mcp_servers"] is None
    assert client.create_kwargs["excluded_tools"] is None


# --- Raw premium-request usage capture (#80, scope-guarded) -------------------------


def _usage_with_quota(used: int, *, quota: str = "premium_interactions") -> Any:
    return AssistantUsageData.from_dict(
        {
            "model": "auto",
            "quotaSnapshots": {
                quota: {
                    "entitlementRequests": 300,
                    "isUnlimitedEntitlement": False,
                    "overage": 0.0,
                    "overageAllowedWithExhaustedQuota": False,
                    "remainingPercentage": 90.0,
                    "usageAllowedWithExhaustedQuota": True,
                    "usedRequests": used,
                }
            },
        }
    )


def test_read_premium_requests_extracts_used_counts() -> None:
    counts = read_premium_requests(_usage_with_quota(42))
    assert counts == {"premium_interactions": 42}


def test_read_premium_requests_fails_safe_without_snapshots() -> None:
    # Copilot quota often reports no snapshot; the accessor degrades to {}, not a raise.
    assert read_premium_requests(AssistantUsageData(model="auto")) == {}


def test_read_premium_requests_skips_reshaped_snapshot() -> None:
    # The count lives on an internal, unstable SDK field; a snapshot missing it is
    # skipped, not fatal — the accessor isolates that instability (scope guard #80).
    class _Reshaped:
        pass  # no _used_requests attribute

    usage = AssistantUsageData(model="auto")
    usage._quota_snapshots = cast(Any, {"premium_interactions": _Reshaped()})

    assert read_premium_requests(usage) == {}


async def test_turn_records_raw_premium_requests() -> None:
    # AC2: raw usage counts are recorded per turn — captured off the usage event onto
    # the session (recorded only; NOT wired into the dollar budget, per the guard).
    backend, _client, _session = _backend_with(
        [_usage_with_quota(7), _msg("hi"), SessionIdleData()]
    )
    task = backend.create_session(model="auto")
    assert isinstance(task, CopilotTaskSession)

    [event async for event in task.run_turn("go")]

    assert task.last_premium_requests == {"premium_interactions": 7}


async def test_premium_requests_reset_each_turn() -> None:
    session = FakeCopilotSession(
        "sess-1", [_usage_with_quota(3), _msg("hi"), SessionIdleData()]
    )
    client = FakeCopilotClient(session)
    task = CopilotTaskSession(model="auto", client_factory=lambda: client)

    [event async for event in task.run_turn("first")]
    assert task.last_premium_requests == {"premium_interactions": 3}

    session._script = [_msg("again"), SessionIdleData()]
    [event async for event in task.run_turn("second")]
    assert task.last_premium_requests == {}


# --- Resume self-heal on a dead pointer (#80, parity with TaskSession) --------------


class _DeadResumeClient:
    """A client whose resume attempt fails once, then a fresh create succeeds."""

    def __init__(self, session: FakeCopilotSession, error: Exception) -> None:
        self._session = session
        self._error = error
        self.create_kwargs: dict[str, Any] | None = None
        self.resume_args: tuple[str, dict[str, Any]] | None = None
        self.stopped = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True

    @property
    def rpc(self) -> _FakeServerRpc:
        return _FakeServerRpc(cast(FakeCopilotClient, self))

    async def create_session(self, **kwargs: Any) -> FakeCopilotSession:
        self.create_kwargs = kwargs
        return self._session

    async def resume_session(
        self, session_id: str, **kwargs: Any
    ) -> FakeCopilotSession:
        self.resume_args = (session_id, kwargs)
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        JsonRpcError(code=-32000, message="unknown session"),
        ProcessExitedError("runtime exited"),
    ],
)
async def test_dead_resume_self_heals_onto_fresh_session(error: Exception) -> None:
    # AC2: a dead/expired resume id drops the pointer and retries once on a fresh
    # session rather than crashing the turn — mirrors TaskSession's self-heal. The same
    # client resumes (fails) then creates (succeeds); the reconnect asks for it again.
    fresh = FakeCopilotSession("fresh-id", [_msg("healed"), SessionIdleData()])
    client = _DeadResumeClient(fresh, error)

    task = CopilotTaskSession(
        model="auto", resume="stale-id", client_factory=lambda: client
    )

    events = [event async for event in task.run_turn("continue")]

    assert events == [Final(text="healed")]
    assert client.resume_args is not None  # the resume was genuinely attempted first
    assert client.create_kwargs is not None  # then healed onto a fresh create
    assert task.session_id == "fresh-id"


async def test_dead_resume_reraises_when_not_resuming() -> None:
    # A create-path (no resume) failure has nothing to self-heal to — it surfaces.
    session = FakeCopilotSession("sess-1", [])

    class _FailingCreateClient(_DeadResumeClient):
        async def create_session(self, **kwargs: Any) -> FakeCopilotSession:
            raise self._error

    clients = iter(
        [_FailingCreateClient(session, ProcessExitedError("boom"))]
    )
    task = CopilotTaskSession(model="auto", client_factory=lambda: next(clients))

    with pytest.raises(ProcessExitedError):
        [event async for event in task.run_turn("go")]


# --- Concurrency model: one runtime per session (#80 AC3) --------------------------


async def test_parallel_chats_get_independent_sessions() -> None:
    # AC3: chief runs a session per chat. The backend builds one CopilotTaskSession per
    # create_session call, each spinning up its own client/runtime (parity with
    # ClaudeBackend, no shared-client locking) — so two chats run without cross-talk.
    made: list[FakeCopilotClient] = []

    def factory() -> FakeCopilotClient:
        n = len(made) + 1
        client = FakeCopilotClient(
            FakeCopilotSession(f"sess-{n}", [_msg(f"reply {n}"), SessionIdleData()])
        )
        made.append(client)
        return client

    backend = CopilotBackend(client_factory=factory)
    task_a = backend.create_session(model="auto")
    task_b = backend.create_session(model="auto")

    events_a = [event async for event in task_a.run_turn("chat A")]
    events_b = [event async for event in task_b.run_turn("chat B")]

    assert events_a == [Final(text="reply 1")]
    assert events_b == [Final(text="reply 2")]
    assert len(made) == 2  # a distinct runtime per chat, not one shared client
    assert made[0] is not made[1]
    assert task_a.session_id == "sess-1"
    assert task_b.session_id == "sess-2"
