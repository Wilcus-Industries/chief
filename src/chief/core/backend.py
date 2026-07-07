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
    :class:`~chief.core.session.SessionProto`.
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
    ) -> SessionProto:
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

    This slice wires ``model``, ``resume``, ``cwd``, and — since #78 — ``system_prompt``
    (mapped onto the Copilot SDK's ``customize``-mode system message; see
    :func:`~chief.core.copilot_session.build_persona_system_message`). The remaining
    tool/gate kwargs (``can_use_tool``, ``hooks``, ``allowed_tools`` /
    ``disallowed_tools``, ``mcp_servers``, ``plugins``, ``skills``, ``fork_session``)
    are accepted to satisfy the :class:`AgentBackend` contract but not yet forwarded;
    later #72 slices map them onto the Copilot SDK's permission callback, hooks, and
    custom tools.
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
    ) -> SessionProto:
        return CopilotTaskSession(
            model=model,
            resume=resume,
            cwd=cwd,
            system_prompt=system_prompt,
            client_factory=self._client_factory,
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
