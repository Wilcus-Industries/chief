"""Subagents: named agent definitions, their tool filter, and the spawn tool.

A definition is a markdown file in the agents dir (YAML frontmatter: name,
description, optional tools allowlist and model; body: its system prompt).
A spawn starts a detached run (``chief.agent.subagent_pool``) that talks to its
parent over ``send_msg``; ``wait=true`` keeps the old blocking form. Subagents
can never spawn further subagents.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from chief.provider.base import ToolCall, ToolSpec
from chief.tools import Tool, ToolContext, ToolDispatcher, ToolRegistry

if TYPE_CHECKING:  # the pool imports this module's AgentDef/FilteredTools
    from chief.agent.subagent_pool import SubagentPool

# Prepended to every definition's prompt (PRD #308): runs are detached, and
# messaging is for questions and blockers, never status chatter.
PREAMBLE = (
    "You are a subagent: you run detached from the session that spawned you "
    "(your parent), which carries on with its own work meanwhile. Use "
    "`send_msg` (to your parent, the default) only to ask a question you "
    "can't answer yourself or to report a blocker — never for status updates "
    "or acknowledgements. If you never message your parent, your final reply "
    "is your result. Once you have messaged your parent, you must call "
    "`finish(result)` when you are done; ending a reply without it leaves you "
    "waiting for the parent's answer."
)


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None  # None = every tool (minus spawn)
    model: str | None


class AgentRegistry:
    """Scans the agents dir for definitions."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def scan(self) -> list[AgentDef]:
        return [d for f in sorted(self._root.glob("*.md")) if (d := _parse(f))]

    def get(self, name: str) -> AgentDef | None:
        return next((d for d in self.scan() if d.name == name), None)


def _parse(path: Path) -> AgentDef | None:
    text = path.read_text()
    if not text.startswith("---"):
        return None
    _, frontmatter, body = text.split("---", 2)
    meta = yaml.safe_load(frontmatter) or {}
    tools = meta.get("tools")
    return AgentDef(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or "").strip(),
        system_prompt=body.strip(),
        tools=tuple(tools) if tools else None,
        model=meta.get("model"),
    )


class FilteredTools:
    """A ToolDispatcher restricted to an allowlist (spawn always excluded).

    For a subagent, ``send_msg`` is always allowed whatever the allowlist
    says — otherwise an allowlisted agent could never reach its parent."""

    def __init__(
        self, inner: ToolDispatcher, allow: tuple[str, ...] | None,
        subagent: bool = False,
    ) -> None:
        self._inner = inner
        self._allow = allow
        self._always = frozenset({"send_msg"}) if subagent else frozenset()

    def _allowed(self, name: str) -> bool:
        if name == "spawn_agent":
            return False
        return self._allow is None or name in self._allow or name in self._always

    def specs(self) -> list[ToolSpec]:
        return [s for s in self._inner.specs() if self._allowed(s.name)]

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        if not self._allowed(call.name):
            return f"error: tool '{call.name}' not allowed for this agent"
        return await self._inner.dispatch(call, context)


class SubagentScope:
    """The registry under a subagent's gate. The gate uses the run's context
    (parent + run id) for cards and audit; only ``send_msg`` and MCP tools
    (an opted-in server must see the parent's session) receive it. Native
    tools run context-free: a subagent must not move its parent's shell cwd,
    switch its model, or schedule as it."""

    def __init__(self, inner: ToolDispatcher) -> None:
        self._inner = inner

    def specs(self) -> list[ToolSpec]:
        return self._inner.specs()

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        if call.name != "send_msg" and not call.name.startswith("mcp_"):
            context = None
        return await self._inner.dispatch(call, context)


_SPAWN_SPEC = ToolSpec(
    name="spawn_agent",
    description=(
        "Run a named subagent on a task, detached. action=spawn (default) "
        "needs `name` and `task` and returns the subagent's id at once; it "
        "works while you carry on, and its result arrives later as a message "
        "from it. It may message you a question — answer with "
        "send_msg(to=<its id>). With `wait`=true the call blocks and returns "
        "the result instead, or returns early with the subagent's first "
        "question, after which it runs on async. action=list shows your "
        "running subagents; action=cancel needs `id` and stops one of them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["spawn", "list", "cancel"]},
            "name": {"type": "string"},
            "task": {"type": "string"},
            "wait": {"type": "boolean"},
            "id": {"type": "string", "description": "cancel: the subagent id"},
        },
    },
)

TASK_PREVIEW_CHARS = 60


def describe(run: Any, now: float) -> str:
    """One ``/tasks`` / ``action=list`` line for a live run (a SubagentRun;
    typed loosely to avoid the import cycle with the pool)."""
    state = "running" if run.running else "idle"
    task = run.task
    if len(task) > TASK_PREVIEW_CHARS:
        task = task[:TASK_PREVIEW_CHARS] + "…"
    age = int(now - run.started)
    parent = run.parent_key
    return f"{run.id} [{run.agent.name}] parent={parent} {state} {age}s — {task}"


def describe_all(runs: list[Any]) -> str:
    """Every line, or the empty-list answer."""
    now = time.monotonic()
    return "\n".join(describe(r, now) for r in runs) or "no subagents running"


def register_spawn_tool(
    registry: ToolRegistry, agents: AgentRegistry, pool: "SubagentPool"
) -> None:
    """Expose spawn_agent (spawn/list/cancel), scoped to the caller's own runs."""

    async def spawn_agent(
        context: ToolContext | None, action: str = "spawn", name: str = "",
        task: str = "", wait: bool = False, id: str = "",  # noqa: A002 - tool arg
    ) -> str:
        if context is None:
            return "error: spawn_agent needs a session context"
        parent = context.thread_key
        if action == "list":
            return describe_all(pool.live(parent))
        if action == "cancel":
            run = pool.get(id)
            if run is None or run.parent_key != parent:
                return f"error: no running subagent of yours named '{id}'"
            await pool.cancel(id, by="parent")
            return f"cancelled {id}"
        if action != "spawn":
            return f"error: unknown action '{action}' (spawn, list, cancel)"
        definition = agents.get(name)
        if definition is None:
            known = ", ".join(d.name for d in agents.scan()) or "none"
            return f"error: no agent named '{name}' (known: {known})"
        return await pool.spawn(parent, context.channel, definition, task, wait)

    registry.register(Tool(_SPAWN_SPEC, spawn_agent, wants_context=True))
