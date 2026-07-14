"""Persistent per-task Copilot-SDK session (issue #76, part of #72).

The Claude twin of this module is :mod:`chief.core.session`; this one drives the
**GitHub Copilot SDK** (import name ``copilot``) behind the same
:class:`~chief.core.session.SessionProto` the engine builds every session against. The
central mechanism is a *real* Copilot-SDK turn: create a streaming session and run one
owner turn to a reply on a Copilot-quota model.

**Event-model rewrite.** claude-agent-sdk hands the session a linear async stream of
typed messages; the Copilot SDK instead delivers a firehose of 40+ event types through a
**callback** (``session.on(handler)``) and signals turn completion out-of-band. So this
module bridges that push model onto ``run_turn``'s pull model — a registered handler
pumps every :class:`SessionEvent` onto an :class:`asyncio.Queue`, and ``run_turn``
drains the queue, mapping the handful of events chief cares about onto ``Milestone`` /
``Final`` and re-deriving the turn boundary:

* ``assistant.message`` (:class:`AssistantMessageData`) → one ``Final`` per message, so
  the engine surfaces each block to the owner individually (per-block streaming, #64) —
  the analogue of claude-agent-sdk's ``TextBlock`` → ``Final``.
* ``tool.execution_start`` (:class:`ToolExecutionStartData`) → ``Milestone("using …")``,
  the analogue of ``ToolUseBlock`` → ``Milestone``.
* ``subagent.started`` (:class:`SubagentStartedData`) → ``Milestone("delegating to …")``
  (#87) — surfaces a category-routed subagent turn *and* the model it runs on, so the
  owner sees the delegation and its resolved model.
* ``assistant.usage`` (:class:`AssistantUsageData`) → ``last_cost_usd`` (summed across
  the turn's model calls; ``0.0`` on Copilot quota, which reports no dollar cost),
  ``last_served_model`` (``data.model`` — the actually-served model, #90), and
  ``last_premium_requests`` (raw per-quota used-request counts, #80 — see
  :func:`read_premium_requests`; summed into the premium-request budget currency, #84).
* ``session.limits_exhausted`` → ``last_rate_limit_status = "rejected"`` so the budget
  gate can back off — Copilot's quota model has no per-turn allowed/warning status like
  claude-agent-sdk's ``RateLimitEvent``, so ``rejected`` is the only synthesised signal.
* ``session.idle`` (:class:`SessionIdleData`) is the turn boundary — **not**
  ``assistant.turn_end``, which fires once per inner tool round-trip (#72 spike).
* ``session.error`` / ``model.call_failure`` → raise, aborting the turn.

The permission gate is wired here (#77, part of #72): the backend hands this session a
Copilot ``on_permission_request`` handler and ``SessionHooks`` (built from chief's gate
by :mod:`chief.core.copilot_gate`), and :meth:`CopilotTaskSession._ensure_connected`
threads them into the live session. They are non-persisted SDK callbacks, so **both**
the create and the resume branch re-register them on every connect.

**Tools + MCP servers (#80, part of #72).** chief's mixed ``mcp_servers`` mapping is
split at connect by :func:`~chief.core.copilot_tools.partition_mcp_servers` into the
SDK's two tool inputs — flat custom ``tools`` (the in-process shell/scheduler/guest
servers, converted in-process) and HTTP ``mcp_servers`` (the Google/browser containers,
passed through) — and re-supplied on every connect (the SDK persists neither).
``disallowed_tools`` maps to the SDK's ``excluded_tools``. Resume self-heals onto a
fresh session on a dead pointer, mirroring :class:`TaskSession`.

**Persona via section-customization (#78).** chief's system prompt
(:func:`chief.core.personas.build_system_prompt`) is a single flat string built for
claude-agent-sdk's plain ``system_prompt=``. The Copilot SDK instead structures its
system prompt as twelve named sections (``SystemMessageSection``) and offers a
``customize`` mode that overrides individual sections while keeping the rest of the
SDK-managed prompt. :func:`build_persona_system_message` maps chief's flat string onto
that customize config — see its docstring for which sections carry chief's persona vs.
which are deliberately preserved (the "non-replaceable section" documentation #78
calls for). :func:`find_vendor_identity_leak` is the companion check: a scan for
GitHub Copilot's own self-identification surfacing in a reply despite the
customization.

**Provider target classes (#90, part of #72).** ``provider`` is an optional
:class:`~copilot.ProviderConfig` BYOK override — ``None`` keeps the session on plain
Copilot quota (the ``copilot`` target class); :func:`openrouter_provider_config` builds
one for the ``openrouter`` target class (the SDK's "openai" provider pointed at
OpenRouter, with a concrete model requested via the existing ``model=`` kwarg).

**Subagents + skills (#87, part of #72).** ``custom_agents`` are category-routed
subagents (built owner-only by :func:`chief.core.subagents.build_custom_agents`, each
agent's model already resolved through the routing table) and ``skill_directories`` are
the ported M10 skill dirs. Both are non-persisted SDK inputs, so — like the tools and
gate callbacks — they are re-supplied on **every** connect (create and resume);
``enable_skills`` is turned on exactly when skill dirs are present.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

from copilot import CopilotClient, ProviderConfig, SessionHooks, Tool
from copilot._jsonrpc import JsonRpcError, ProcessExitedError
from copilot.generated.rpc import SessionsForkRequest, SessionsForkResult
from copilot.session import (
    CustomAgentConfig,
    MCPServerConfig,
    SectionOverride,
    SystemMessageConfig,
    SystemMessageSection,
)
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantUsageData,
    ModelCallFailureData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
    SessionLimitsExhaustedRequestedData,
    SubagentStartedData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
)

from ..adapters.base import Attachment
from ..config import Settings
from .copilot_gate import PermissionHandlerFn
from .copilot_tools import partition_mcp_servers
from .session import NO_REPLY, Delta, Final, Milestone, ToolEnd, ToolStart, TurnEvent

logger = logging.getLogger("chief.core.copilot_session")


class CopilotTurnError(RuntimeError):
    """A Copilot turn ended on a session error / model-call failure event."""


class _CopilotSession(Protocol):
    """The slice of ``copilot.CopilotSession`` this module uses (structural)."""

    session_id: str

    def on(
        self, handler: Callable[[SessionEvent], None]
    ) -> Callable[[], None]: ...
    async def send(self, prompt: str, *, attachments: Any = ...) -> str: ...
    async def set_model(self, model: str) -> None: ...
    async def abort(self) -> None: ...
    async def disconnect(self) -> None: ...


class _CopilotSessionsRpc(Protocol):
    """The slice of the SDK's experimental ``sessions.*`` RPC group used here (#93).

    Only :meth:`fork` is reached — ``sessions.fork`` creates a NEW session from an
    existing one's persisted history and returns its id, leaving the source untouched.
    That is how :meth:`chief.core.tasks.TaskManager.branch`'s fork request is honoured
    on Copilot: the branch resumes the forked copy, so the casual channel keeps its own
    session. The group is marked *Experimental* by the SDK; isolating it behind this one
    slot keeps the churn contained if the RPC shifts.
    """

    async def fork(
        self, params: SessionsForkRequest, *, timeout: float | None = ...
    ) -> SessionsForkResult: ...


class _CopilotServerRpc(Protocol):
    """The slice of ``copilot`` ``ServerRpc`` (``client.rpc``) this module reaches."""

    @property
    def sessions(self) -> _CopilotSessionsRpc: ...


class _CopilotClient(Protocol):
    """The slice of ``copilot.CopilotClient`` this module uses (structural).

    The third-party boundary: production spawns the real runtime, tests inject a fake so
    a real turn can be dispatched through the session without a subprocess.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def force_stop(self) -> None: ...

    @property
    def rpc(self) -> _CopilotServerRpc: ...

    async def create_session(
        self,
        *,
        model: str | None = ...,
        working_directory: str | None = ...,
        on_permission_request: PermissionHandlerFn | None = ...,
        hooks: SessionHooks | None = ...,
        system_message: SystemMessageConfig | None = ...,
        provider: ProviderConfig | None = ...,
        tools: list[Tool] | None = ...,
        mcp_servers: dict[str, MCPServerConfig] | None = ...,
        excluded_tools: list[str] | None = ...,
        custom_agents: list[CustomAgentConfig] | None = ...,
        skill_directories: list[str] | None = ...,
        enable_skills: bool | None = ...,
    ) -> _CopilotSession: ...
    async def resume_session(
        self,
        session_id: str,
        *,
        model: str | None = ...,
        working_directory: str | None = ...,
        on_permission_request: PermissionHandlerFn | None = ...,
        hooks: SessionHooks | None = ...,
        system_message: SystemMessageConfig | None = ...,
        provider: ProviderConfig | None = ...,
        tools: list[Tool] | None = ...,
        mcp_servers: dict[str, MCPServerConfig] | None = ...,
        excluded_tools: list[str] | None = ...,
        custom_agents: list[CustomAgentConfig] | None = ...,
        skill_directories: list[str] | None = ...,
        enable_skills: bool | None = ...,
    ) -> _CopilotSession: ...


CopilotClientFactory = Callable[[], _CopilotClient]


def _default_copilot_client() -> _CopilotClient:
    return CopilotClient()


#: Sections that carry GitHub Copilot's own vendor identity and voice: the "who am I"
#: preamble, the identity section, and its tone rules. chief's persona replaces these
#: outright — a real identity swap, not an addition alongside the vendor's.
_IDENTITY_REPLACED: SystemMessageSection = "identity"
_VOICE_SECTIONS_REMOVED: tuple[SystemMessageSection, ...] = ("preamble", "tone")

#: Sections deliberately PRESERVED — the "non-replaceable section" documentation #78
#: calls for. These are Copilot's own operational scaffolding (how to use tools
#: correctly, environment awareness, safety guardrails, repo/runtime context), not
#: vendor *identity* — stripping them risks breaking tool use for no persona benefit,
#: per the issue's own guidance. Because there is no SDK-enforced non-removable core,
#: this list is a project decision, not a platform constraint: full-identity-replacement
#: fidelity is otherwise unverified, so :func:`find_vendor_identity_leak` is the safety
#: net that catches vendor voice bleeding through these sections in an actual reply.
_PRESERVED_SECTIONS: tuple[SystemMessageSection, ...] = (
    "tool_efficiency",
    "environment_context",
    "code_change_rules",
    "guidelines",
    "safety",
    "tool_instructions",
    "custom_instructions",
    "runtime_instructions",
    "last_instructions",
)


def build_persona_system_message(
    system_prompt: str | None,
) -> SystemMessageConfig | None:
    """Map chief's flat persona string onto a Copilot ``customize`` system message.

    ``None``/empty input means no override (the SDK's default prompt). Otherwise
    returns a ``customize``-mode config (never the blunt ``replace`` mode, which drops
    the SDK's own guardrails entirely): ``identity`` is replaced with ``system_prompt``
    verbatim, ``preamble`` and ``tone`` are removed (chief's content sets its own
    opening and voice), and :data:`_PRESERVED_SECTIONS` are marked ``preserve`` so
    Copilot's tool-use/safety/environment scaffolding survives untouched. The caller
    (``system_prompt``) is already tier-scoped by
    :func:`chief.core.personas.build_system_prompt` — this mapping is content-blind, so
    a guest persona in means a guest customize config out.
    """
    if not system_prompt:
        return None
    sections: dict[SystemMessageSection, SectionOverride] = {
        _IDENTITY_REPLACED: {"action": "replace", "content": system_prompt},
    }
    for name in _VOICE_SECTIONS_REMOVED:
        sections[name] = {"action": "remove"}
    for name in _PRESERVED_SECTIONS:
        sections[name] = {"action": "preserve"}
    return {"mode": "customize", "sections": sections}


#: Vendor self-identification phrases a chief-voiced reply must never contain
#: (case-insensitive). Deliberately narrow — the product name alone, or generic
#: "an AI assistant" framing, would false-positive on chief legitimately mentioning
#: the product or describing itself in ordinary English. These target Copilot
#: SPEAKING AS ITSELF.
_VENDOR_IDENTITY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"github copilot",
        r"\bi'?m copilot\b",
        r"\bi am copilot\b",
        r"copilot cli",
        r"as (?:an ai )?(?:created|developed|built|made) by github",
        r"\bcopilot,? (?:an ai|a coding assistant)",
    )
)


def find_vendor_identity_leak(text: str) -> str | None:
    """Return the first vendor-identity phrase found in ``text``, else ``None``.

    The leakage check #78 calls for: a probe turn's reply must speak as chief, never
    as GitHub Copilot. Scans against :data:`_VENDOR_IDENTITY_PATTERNS` — a small,
    deliberately narrow set of self-identification phrasings, not a bare "copilot"
    substring match, so chief mentioning the product by name in passing isn't flagged.
    """
    for pattern in _VENDOR_IDENTITY_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None


#: The OpenRouter BYOK provider target class (#90, part of #72): the SDK's "openai"
#: provider pointed at OpenRouter's OpenAI-compatible endpoint. A concrete OpenRouter
#: model is requested via the session's existing ``model=`` kwarg alongside this.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def openrouter_provider_config(settings: Settings) -> ProviderConfig:
    """Build the ``openrouter`` BYOK :class:`~copilot.ProviderConfig` (#90, part of
    #72).

    ``ProviderConfig`` is a ``total=False`` ``TypedDict`` — ``api_key`` is only set when
    ``settings.openrouter_api_key`` is configured (never hardcoded), so an unconfigured
    key surfaces as an auth error from OpenRouter itself, not a masked empty string.
    Pass the result as ``create_session``'s ``provider=`` kwarg alongside a concrete
    OpenRouter model name in ``model=``.
    """
    config: ProviderConfig = {"base_url": OPENROUTER_BASE_URL, "type": "openai"}
    if settings.openrouter_api_key is not None:
        config["api_key"] = settings.openrouter_api_key
    return config


def read_premium_requests(data: AssistantUsageData) -> dict[str, int]:
    """Raw per-quota premium-request counts from an ``assistant.usage`` event.

    Reads the SDK's ``_quota_snapshots`` — an **internal**, underscore-prefixed field
    (:class:`copilot.session_events.AssistantUsageData`) mapping a quota name (e.g. a
    premium-request pool) to its snapshot, whose ``_used_requests`` is the running count
    used this cycle. Isolated behind this one accessor and **fail-safe**: a missing or
    reshaped field yields ``{}`` (no count), never a crash, since the field is not part
    of the SDK's public surface.

    SCOPE (#80, part of #72): this captures the **raw counts only**. The budget slice
    (#84) sums them (:func:`chief.core.budget.premium_request_total`) into the
    premium-request currency, metered against the monthly cap by
    :class:`~chief.core.budget.BudgetGate`.
    """
    snapshots = getattr(data, "_quota_snapshots", None)
    if not snapshots:
        return {}
    counts: dict[str, int] = {}
    for name, snapshot in snapshots.items():
        used = getattr(snapshot, "_used_requests", None)
        if isinstance(used, int):
            counts[name] = used
    return counts


def _subagent_milestone(data: SubagentStartedData) -> str:
    """Milestone text for a subagent starting (#87) — surfaces the delegation + model.

    The ``subagent.started`` event carries the agent's name and the model it runs on;
    including the model is what makes "runs on the model its category resolves to"
    visible to the owner (the category's resolved model was set on the agent config).
    ``model`` is optional on the event, so it's appended only when present.
    """
    name = data.agent_display_name or data.agent_name
    if data.model:
        return f"delegating to {name} ({data.model})"
    return f"delegating to {name}"


def _describe_error(data: SessionErrorData | ModelCallFailureData) -> str:
    """A one-line reason from an error / failure event, for the raised exception."""
    if isinstance(data, SessionErrorData):
        return f"{data.error_type}: {data.message}"
    reason = data.error_message or data.error_type or "model call failed"
    return f"{data.source}: {reason}"


class CopilotTaskSession:
    """One task's live Copilot-SDK conversation, exposed as a ``SessionProto``.

    Lazily connected: construction is cheap (mirrors :class:`TaskSession`), and the
    first ``run_turn`` spins up the client, opens the session, and registers the event
    handler.
    """

    def __init__(
        self,
        *,
        model: str,
        resume: str | None = None,
        cwd: str | None = None,
        on_permission_request: PermissionHandlerFn | None = None,
        hooks: SessionHooks | None = None,
        system_prompt: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        disallowed_tools: list[str] | None = None,
        client_factory: CopilotClientFactory = _default_copilot_client,
        provider: ProviderConfig | None = None,
        fork_session: bool = False,
        custom_agents: list[CustomAgentConfig] | None = None,
        skill_directories: list[str] | None = None,
    ) -> None:
        self._model = model
        self._resume = resume
        #: When resuming, fork the source session first so the branch gets an
        #: independent copy instead of driving (and mutating) the casual channel's live
        #: session (#93, part of #72). Set by
        #: :meth:`chief.core.tasks.TaskManager.branch`; ignored on a plain create or
        #: resume.
        self._fork_session = fork_session
        #: The SDK refuses a relative directory ("Directory path must be absolute:
        #: data/memory") and fails the turn at session.create. chief's own defaults are
        #: relative (``memory_dir``, ``chief_skills_dir``), so resolve here — the single
        #: boundary every caller crosses — against the process cwd, the same convention
        #: ``config.yaml`` and ``SKILLS_PLUGIN_DIR`` are resolved by.
        self._cwd = os.path.abspath(cwd) if cwd is not None else None
        #: chief's gate, adapted onto the Copilot boundary by the backend. Non-persisted
        #: SDK callbacks, so re-registered on every connect (create *and* resume) below.
        self._on_permission_request = on_permission_request
        self._hooks = hooks
        #: Built once from ``system_prompt`` (issue #78) — see
        #: :func:`build_persona_system_message` for the section mapping.
        self._system_message = build_persona_system_message(system_prompt)
        #: chief's mixed ``mcp_servers`` mapping (#80) — split at connect into flat
        #: Copilot tools (in-process shell/scheduler/guest) + HTTP MCP servers (Google/
        #: browser) by :func:`~chief.core.copilot_tools.partition_mcp_servers`.
        self._mcp_servers = mcp_servers
        #: Hard-refused tool names → the SDK's ``excluded_tools`` (hidden from the
        #: model). chief's ``allowed_tools`` is intentionally NOT mapped to the SDK's
        #: ``available_tools`` — it is a pre-approval list the gate honours (approve-
        #: once), not a visibility allowlist; forwarding it would hide the shell/
        #: schedule tools chief deliberately keeps off the allowlist so they route
        #: through the gate. See :class:`~chief.core.backend.CopilotBackend`.
        self._disallowed_tools = disallowed_tools
        self._client_factory = client_factory
        #: BYOK provider config (e.g. :func:`openrouter_provider_config`) — ``None``
        #: keeps the session on plain Copilot quota (#90, part of #72).
        self._provider = provider
        #: Category-routed subagents (#87) — owner-declared specs whose model was
        #: already resolved through the routing table by
        #: :func:`chief.core.subagents.build_custom_agents`. Non-persisted, so
        #: re-supplied on every connect (create *and* resume), like the tool surface.
        #: Owner-only: guests are handed ``None`` at the wiring layer.
        self._custom_agents = custom_agents
        #: Absolute for the same reason ``_cwd`` is — and doubly so: the SDK resolves a
        #: relative skill dir against the session's cwd (``memory_dir``), not chief's.
        #: Ported M10 skill directories (#87) — the Copilot analogue of chief's plugin +
        #: ``skills=`` filter. Non-empty turns skills on for this session; also
        #: re-supplied on every connect.
        self._skill_directories = (
            [os.path.abspath(d) for d in skill_directories]
            if skill_directories
            else skill_directories
        )
        self._client: _CopilotClient | None = None
        self._session: _CopilotSession | None = None
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Per-turn event sink; the ``on`` handler pumps events here and ``run_turn``
        #: drains it. Recreated each turn so a stray connect-time event can't bleed in.
        self._queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        #: Resumable session id, captured from the live session on connect.
        self.session_id: str | None = resume
        #: This turn's summed SDK cost; reset each turn (mirrors :class:`TaskSession`).
        self.last_cost_usd: float = 0.0
        #: Latest synthesised rate-limit status — only ``rejected`` (on quota
        #: exhaustion); Copilot exposes no allowed/warning status. Sticky across turns.
        self.last_rate_limit_status: str | None = None
        #: The actually-served model, captured from the latest usage event's
        #: ``data.model`` (#90) — Copilot's ``auto`` and BYOK targets like OpenRouter
        #: both report it here regardless of what was requested. Reset each turn.
        self.last_served_model: str | None = None
        #: This turn's raw premium-request counts (quota name → used-requests), captured
        #: from usage events via :func:`read_premium_requests` (#80). Reset each turn;
        #: the engine sums them into the premium-request currency (#84).
        self.last_premium_requests: dict[str, int] = {}

    def _on_event(self, event: SessionEvent) -> None:
        """Pump one SDK event onto the current turn's queue (thread-safe).

        The SDK may dispatch handlers off the event loop, so hop back onto it with
        ``call_soon_threadsafe`` — safe whether the callback fires on- or off-loop.
        """
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        self._loop = asyncio.get_running_loop()
        client = self._client_factory()
        await client.start()
        # chief's mixed mcp_servers → the SDK's two tool inputs (#80): in-process
        # shell/scheduler/guest become flat custom `tools`, HTTP Google/browser servers
        # pass through as `mcp_servers`. disallowed_tools → excluded_tools (hidden).
        tools, http_servers = await partition_mcp_servers(self._mcp_servers)
        excluded = self._disallowed_tools or None
        # Subagents + skill dirs (#87) are non-persisted too, so re-supplied on every
        # connect alongside the tools. `enable_skills` is turned on only when chief
        # hands skill dirs — an empty-mode session otherwise defaults skills off (SDK).
        custom_agents = self._custom_agents or None
        skill_dirs = self._skill_directories or None
        enable_skills = True if skill_dirs else None
        # The gate callbacks are non-persisted, so both branches re-register them fresh;
        # a resumed session is gated identically to a freshly created one (#77). The
        # tool surface is likewise re-supplied on resume — the SDK does not persist it.
        if self._resume is not None:
            # A branch (fork_session) forks the casual channel's persisted history into
            # a NEW session and resumes that copy, so the two threads stay independent
            # (#93, part of #72). A plain resume reopens the id directly.
            resume_id = self._resume
            if self._fork_session:
                forked = await client.rpc.sessions.fork(
                    SessionsForkRequest(session_id=self._resume)
                )
                resume_id = forked.session_id
            session = await client.resume_session(
                resume_id,
                model=self._model,
                working_directory=self._cwd,
                on_permission_request=self._on_permission_request,
                hooks=self._hooks,
                system_message=self._system_message,
                provider=self._provider,
                tools=tools or None,
                mcp_servers=http_servers or None,
                excluded_tools=excluded,
                custom_agents=custom_agents,
                skill_directories=skill_dirs,
                enable_skills=enable_skills,
            )
        else:
            session = await client.create_session(
                model=self._model,
                working_directory=self._cwd,
                on_permission_request=self._on_permission_request,
                hooks=self._hooks,
                system_message=self._system_message,
                provider=self._provider,
                tools=tools or None,
                mcp_servers=http_servers or None,
                excluded_tools=excluded,
                custom_agents=custom_agents,
                skill_directories=skill_dirs,
                enable_skills=enable_skills,
            )
        session.on(self._on_event)
        self._client = client
        self._session = session
        self.session_id = session.session_id
        self._connected = True

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        """Send ``text``, stream the response as ``Milestone`` / ``Final`` events.

        A dead/expired resume id can make the connect or first send fail. If nothing
        has streamed yet on the connecting turn and we were resuming, drop the resume
        pointer and retry once on a fresh session rather than crashing — the new id is
        then persisted by the engine, healing the stale pointer (mirrors
        :meth:`TaskSession.run_turn`). Later turns (already connected) never self-heal,
        so a mid-conversation fault surfaces instead of orphaning the transcript.
        """
        needs_connect = not self._connected
        streamed = False
        try:
            async for event in self._stream_once(text, attachments):
                streamed = True
                yield event
        except (JsonRpcError, ProcessExitedError) as exc:
            if streamed or self._resume is None or not needs_connect:
                raise
            logger.warning(
                "resume failed (%s); retrying on a fresh session", exc
            )
            await self._reset_to_fresh()
            async for event in self._stream_once(text, attachments):
                yield event

    async def _stream_once(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> AsyncIterator[TurnEvent]:
        """One streamed turn against the current session (no resume fallback).

        Drains the event queue until ``session.idle`` (the turn boundary), mapping each
        event chief cares about. A turn that streams no assistant text still yields one
        ``Final(NO_REPLY)`` so callers never see an empty stream (mirrors
        :class:`TaskSession`).
        """
        await self._ensure_connected()
        assert self._session is not None  # set by _ensure_connected
        if attachments:
            # This slice is text-only; attachment/image forwarding is a later #72 slice
            # (the SDK's send() takes attachments, PDFs are pre-extracted upstream).
            logger.warning(
                "CopilotTaskSession dropped %d attachment(s) — not wired yet (#72)",
                len(attachments),
            )
        self._queue = asyncio.Queue()
        self.last_cost_usd = 0.0  # this turn's spend only; the engine sums per turn
        self.last_served_model = None  # this turn's served model; reset each turn
        self.last_premium_requests = {}  # this turn's raw quota counts; reset each turn
        text_blocks_seen = 0
        await self._session.send(text)
        while True:
            data = (await self._queue.get()).data
            if isinstance(data, SessionIdleData):
                break
            if isinstance(data, AssistantMessageData):
                # The done delta precedes the Final so a live view can retire its
                # accumulated deltas before the authoritative block arrives.
                yield Delta(message_id=data.message_id, text="", done=True)
                if data.content:
                    text_blocks_seen += 1
                    yield Final(text=data.content)
            elif isinstance(data, AssistantMessageDeltaData):
                if data.delta_content:
                    yield Delta(
                        message_id=data.message_id, text=data.delta_content
                    )
            elif isinstance(data, SubagentStartedData):
                yield Milestone(text=_subagent_milestone(data))
            elif isinstance(data, ToolExecutionStartData):
                yield ToolStart(
                    tool_call_id=data.tool_call_id, name=data.tool_name
                )
            elif isinstance(data, ToolExecutionCompleteData):
                yield ToolEnd(
                    tool_call_id=data.tool_call_id,
                    ok=data.success,
                    detail=data.error.message if data.error else "",
                )
            elif isinstance(data, AssistantUsageData):
                if data.cost is not None:
                    self.last_cost_usd += data.cost
                self.last_served_model = data.model
                counts = read_premium_requests(data)
                if counts:
                    self.last_premium_requests.update(counts)
            elif isinstance(data, SessionLimitsExhaustedRequestedData):
                self.last_rate_limit_status = "rejected"
            elif isinstance(data, (SessionErrorData, ModelCallFailureData)):
                raise CopilotTurnError(_describe_error(data))
        if text_blocks_seen == 0:
            yield Final(text=NO_REPLY)

    async def _reset_to_fresh(self) -> None:
        """Tear down the failed resuming session and rebuild with no resume pointer."""
        try:
            await self.aclose()
        except Exception:  # the runtime is already down; disconnect/stop may error
            logger.debug("aclose during resume reset failed", exc_info=True)
        self._resume = None
        self.session_id = None
        self._connected = False

    async def interrupt(self) -> None:
        """Abort the in-flight turn (steering 'stop / do X instead')."""
        if self._session is not None:
            await self._session.abort()

    async def set_model(self, model: str) -> None:
        """Switch the model — live if connected, else for the next connect."""
        self._model = model
        if self._session is not None:
            await self._session.set_model(model)

    async def aclose(self) -> None:
        """Disconnect the session and stop the client, freeing the runtime.

        Unbounded on a wedged runtime: ``disconnect`` sends ``session.destroy`` with no
        timeout and ``stop`` awaits more RPCs, so a wedged CLI never answers and a
        time-boxed caller that cancels this mid-teardown skips the subprocess terminate
        at the tail of ``stop`` — orphaning the Copilot CLI (#101). :meth:`force_close`
        is the bounded escape hatch for those time-boxed callers.
        """
        if self._session is not None:
            await self._session.disconnect()
            self._session = None
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._connected = False

    async def force_close(self) -> None:
        """Bounded, ungraceful teardown for a wedged session (#101).

        ``aclose`` awaits unbounded RPCs (``disconnect`` sends ``session.destroy`` with
        no timeout), so a time-boxed caller cancels it mid-teardown — skipping
        ``client.stop()``'s terminate and leaking the Copilot CLI subprocess. The SDK's
        ``force_stop`` ``kill()``s the spawned CLI without graceful cleanup and is
        itself bounded (a non-blocking kill; its only awaits are ~1s jsonrpc thread
        joins), so it can never re-wedge the caller. It does not ``wait()`` the killed
        child, so the dead process lingers as a transient zombie (one PID, no CPU) until
        Python's subprocess machinery reaps it — a deliberate trade to stay on the
        public ``force_stop`` surface rather than reach into ``client._cli_process`` in
        prod.
        """
        client = self._client
        self._session = None
        self._client = None
        self._connected = False
        if client is not None:
            await client.force_stop()
