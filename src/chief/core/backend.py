"""Agent backend seam (issue #75, part of #72 — the strangler scaffold).

:class:`AgentBackend` is the interface the task engine builds every session against. It
constructs a per-task :class:`~chief.core.session.SessionProto` wired with the model,
the resume pointer, the permission callback + pre-tool hook, and the in-process tools +
MCP servers; the returned session runs the streaming turn (``run_turn``) and switches
the model (``set_model``).

:class:`ClaudeBackend` is the incumbent over claude-agent-sdk — it wraps
:class:`~chief.core.session.TaskSession`, so today's live owner/guest turns flow through
the seam unchanged. :class:`CopilotBackend` (#76, part of #72) implements the same
contract over the **GitHub Copilot SDK** and its event model, wrapping
:class:`~chief.core.copilot_session.CopilotTaskSession`. :func:`select_backend` maps the
config name to one; a config flag picks ``claude`` vs ``copilot`` per deployment.
"""

from typing import Any, Protocol

from claude_agent_sdk import CanUseTool, HookMatcher
from claude_agent_sdk.types import HookEvent
from copilot import ProviderConfig
from copilot.session import CustomAgentConfig

from .copilot_gate import build_permission_handler, build_session_hooks
from .copilot_session import (
    CopilotClientFactory,
    CopilotTaskSession,
    _default_copilot_client,
)
from .session import ClientFactory, SessionProto, TaskSession, _default_client

#: The claude-agent-sdk backend (the incumbent).
CLAUDE_BACKEND = "claude"
#: The GitHub Copilot SDK backend (#76, part of #72).
COPILOT_BACKEND = "copilot"


class AgentBackend(Protocol):
    """Builds the per-task session the engine drives (the strangler seam).

    ``create_session`` mirrors the historical ``SessionFactory`` signature so the
    :class:`~chief.core.tasks.TaskManager` boundary stays stable: it takes the model,
    resume pointer, permission callback + pre-tool hook (``can_use_tool`` / ``hooks``),
    and the in-process tools + MCP servers (``allowed_tools`` / ``disallowed_tools`` /
    ``mcp_servers`` / ``plugins`` / ``skills``), and returns a
    :class:`~chief.core.session.SessionProto`. ``provider`` (#90, part of #72) is an
    optional BYOK provider-target override (e.g. OpenRouter) — Copilot-specific, so
    :class:`ClaudeBackend` accepts and ignores it. ``custom_agents`` and
    ``skill_directories`` (#87, part of #72) are the Copilot-shaped category-routed
    subagents and the ported M10 skills directories — likewise Copilot-specific,
    accepted and ignored by :class:`ClaudeBackend` (which carries subagents/skills via
    its own ``plugins`` / ``skills`` path instead).
    """

    def create_session(
        self,
        *,
        model: str,
        resume: str | None = None,
        fork_session: bool = False,
        can_use_tool: CanUseTool | None = None,
        hooks: dict[HookEvent, list[HookMatcher]] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        skills: list[str] | None = None,
        provider: ProviderConfig | None = None,
        custom_agents: list[CustomAgentConfig] | None = None,
        skill_directories: list[str] | None = None,
    ) -> SessionProto: ...


class ClaudeBackend:
    """The incumbent :class:`AgentBackend` over claude-agent-sdk.

    ``create_session`` constructs a :class:`~chief.core.session.TaskSession`, which owns
    the live :class:`ClaudeSDKClient`. ``client_factory`` is the SDK-client seam (the
    third-party boundary): production uses the real client, tests inject a fake so a
    real turn can be dispatched through the backend without a subprocess.
    """

    def __init__(self, *, client_factory: ClientFactory = _default_client) -> None:
        self._client_factory = client_factory

    def create_session(
        self,
        *,
        model: str,
        resume: str | None = None,
        fork_session: bool = False,
        can_use_tool: CanUseTool | None = None,
        hooks: dict[HookEvent, list[HookMatcher]] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        skills: list[str] | None = None,
        provider: ProviderConfig | None = None,
        custom_agents: list[CustomAgentConfig] | None = None,
        skill_directories: list[str] | None = None,
    ) -> SessionProto:
        # `provider`, `custom_agents`, and `skill_directories` are Copilot-shaped
        # concepts (#90 / #87) — claude-agent-sdk carries subagents/skills via `plugins`
        # / `skills` instead, so these are accepted (per AgentBackend) and dropped.
        return TaskSession(
            model=model,
            resume=resume,
            fork_session=fork_session,
            can_use_tool=can_use_tool,
            hooks=hooks,
            system_prompt=system_prompt,
            cwd=cwd,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            mcp_servers=mcp_servers,
            plugins=plugins,
            skills=skills,
            client_factory=self._client_factory,
        )


class CopilotBackend:
    """An :class:`AgentBackend` over the GitHub Copilot SDK (#76, part of #72).

    ``create_session`` constructs a
    :class:`~chief.core.copilot_session.CopilotTaskSession` that owns the live Copilot
    session and maps its event stream onto chief's ``Milestone`` / ``Final`` events.
    ``client_factory`` is the SDK-client seam (the
    third-party boundary): production spawns the real runtime, tests inject a fake so a
    real turn can be dispatched through the backend without a subprocess.

    **Concurrency model (#80).** One Copilot runtime per chat: every
    ``create_session`` builds its own :class:`CopilotTaskSession`, and each connects via
    its own ``client_factory()`` → its own CLI-server subprocess. This is the same
    one-subprocess-per-session shape as :class:`ClaudeBackend` / :class:`TaskSession`,
    so chief's per-chat sessions stay isolated with no shared-client locking — the SDK's
    ability to multiplex N sessions on one client is deliberately unused, keeping
    ``aclose`` a per-session teardown. ``TaskManager``'s ``Semaphore`` (which bounds
    concurrently *generating* turns) is orthogonal and fits this model unchanged.

    The permission gate is wired (#77, part of #72): ``can_use_tool`` and ``hooks`` are
    chief's SDK-agnostic gate callbacks, adapted by :mod:`chief.core.copilot_gate` onto
    the Copilot SDK's ``on_permission_request`` handler and ``SessionHooks`` and passed
    into the session (re-registered on every connect, so resume is gated too). The
    persona is wired too (#78): ``system_prompt`` is mapped onto the Copilot SDK's
    ``customize``-mode system message (see
    :func:`~chief.core.copilot_session.build_persona_system_message`). ``provider``
    (#90, part of #72) is an optional BYOK provider-target override (``None`` stays on
    plain Copilot quota) —
    :func:`~chief.core.copilot_session.openrouter_provider_config` builds one for the
    ``openrouter`` target class.

    Tools + MCP servers are wired (#80, part of #72): chief's mixed ``mcp_servers``
    mapping is threaded into the session and split at connect by
    :func:`~chief.core.copilot_tools.partition_mcp_servers` into the SDK's flat custom
    ``tools`` (the in-process shell/scheduler/guest servers) and HTTP ``mcp_servers``
    (the Google/browser containers). ``disallowed_tools`` becomes the SDK's
    ``excluded_tools``.

    ``fork_session`` is wired (#93, part of #72): the branch request from
    :meth:`chief.core.tasks.TaskManager.branch` forwards through to
    :class:`~chief.core.copilot_session.CopilotTaskSession`, which forks the casual
    channel's persisted history via the SDK's experimental ``sessions.fork`` RPC and
    resumes the fork — so the branched thread gets an independent session id and the two
    threads never share (and corrupt) each other's context.

    Category-routed subagents + M10 skills are wired (#87, part of #72):
    ``custom_agents`` (built by :func:`chief.core.subagents.build_custom_agents` from
    owner-declared, routing-resolved specs) and ``skill_directories`` (the ported M10
    skill dirs) forward through to :class:`CopilotTaskSession` and onto the SDK's
    ``create_session`` / ``resume_session``. Both are owner-only at the wiring layer
    (:meth:`chief.core.tasks.TaskManager._wire_owner_session`) — a guest session carries
    neither.

    Two contract kwargs stay accepted-but-unforwarded, by design:

    * ``allowed_tools`` — chief's pre-approval list, enforced by the gate
      (approve-once), not a visibility allowlist. The SDK's ``available_tools`` is a
      *hard* allowlist that would hide the shell/schedule tools chief deliberately keeps
      off ``allowed_tools`` so they route through the gate, so it stays unset.
    * ``plugins`` / ``skills`` — chief's ``[{"type":"local","path":…}]`` + ``list[str]``
      shape is claude-agent-sdk's plugin-manifest form; the Copilot equivalent is
      ``skill_directories`` (above), so these two stay unforwarded here.
    """

    def __init__(
        self, *, client_factory: CopilotClientFactory = _default_copilot_client
    ) -> None:
        self._client_factory = client_factory

    def create_session(
        self,
        *,
        model: str,
        resume: str | None = None,
        fork_session: bool = False,
        can_use_tool: CanUseTool | None = None,
        hooks: dict[HookEvent, list[HookMatcher]] | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        plugins: list[Any] | None = None,
        skills: list[str] | None = None,
        provider: ProviderConfig | None = None,
        custom_agents: list[CustomAgentConfig] | None = None,
        skill_directories: list[str] | None = None,
    ) -> SessionProto:
        on_permission_request = (
            build_permission_handler(can_use_tool)
            if can_use_tool is not None
            else None
        )
        copilot_hooks = build_session_hooks(hooks) if hooks is not None else None
        return CopilotTaskSession(
            model=model,
            resume=resume,
            fork_session=fork_session,
            cwd=cwd,
            on_permission_request=on_permission_request,
            hooks=copilot_hooks,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            disallowed_tools=disallowed_tools,
            client_factory=self._client_factory,
            provider=provider,
            custom_agents=custom_agents,
            skill_directories=skill_directories,
        )


def select_backend(name: str) -> AgentBackend:
    """Return the :class:`AgentBackend` named by config; raise on an unknown name.

    ``claude`` and ``copilot`` are valid — an unknown name is a config error, never a
    silent fallback. The config validator (:class:`chief.config.Settings`) enforces the
    same allow-list at load time.
    """
    if name == CLAUDE_BACKEND:
        return ClaudeBackend()
    if name == COPILOT_BACKEND:
        return CopilotBackend()
    raise ValueError(
        f"unknown agent_backend {name!r} — expected "
        f"{CLAUDE_BACKEND!r} or {COPILOT_BACKEND!r}."
    )
