"""Agent backend seam (issue #75, part of #72 — the strangler scaffold).

:class:`AgentBackend` is the interface the task engine builds every session against. It
constructs a per-task :class:`~chief.core.session.SessionProto` wired with the model,
the resume pointer, the permission callback + pre-tool hook, and the in-process tools +
MCP servers; the returned session runs the streaming turn (``run_turn``) and switches
the model (``set_model``).

:class:`ClaudeBackend` is the incumbent over claude-agent-sdk — it wraps
:class:`~chief.core.session.TaskSession`, so today's live owner/guest turns flow through
the seam unchanged. Later backends (a Copilot backend, #72) implement the same contract
against a different SDK and event model; :func:`select_backend` maps the config name to
one, and only ``claude`` is valid for now.
"""

from typing import Any, Protocol

from claude_agent_sdk import CanUseTool, HookMatcher
from claude_agent_sdk.types import HookEvent

from .session import ClientFactory, SessionProto, TaskSession, _default_client

#: The only backend wired today; :func:`select_backend` rejects anything else.
CLAUDE_BACKEND = "claude"


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


def select_backend(name: str) -> AgentBackend:
    """Return the :class:`AgentBackend` named by config; raise on an unknown name.

    Only ``claude`` is valid for now — an unknown name is a config error, never a silent
    fallback. A future Copilot backend (#72) registers its name here.
    """
    if name == CLAUDE_BACKEND:
        return ClaudeBackend()
    raise ValueError(
        f"unknown agent_backend {name!r} — only {CLAUDE_BACKEND!r} is supported."
    )
