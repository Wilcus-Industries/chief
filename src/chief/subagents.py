"""Subagents: named agent definitions plus the spawn tool.

A definition is a markdown file in the agents dir (YAML frontmatter: name,
description, optional tools allowlist and model; body: its system prompt).
Spawning runs a sub-session with its own tool loop and returns the final
text. Subagents can never spawn further subagents.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from chief.agent.loop import run_turn
from chief.budget import Budget
from chief.provider.base import Provider, ToolCall, ToolSpec
from chief.tools import Tool, ToolContext, ToolDispatcher, ToolRegistry


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
    """A ToolDispatcher restricted to an allowlist (spawn always excluded)."""

    def __init__(self, inner: ToolDispatcher, allow: tuple[str, ...] | None) -> None:
        self._inner = inner
        self._allow = allow

    def _allowed(self, name: str) -> bool:
        if name == "spawn_agent":
            return False
        return self._allow is None or name in self._allow

    def specs(self) -> list[ToolSpec]:
        return [s for s in self._inner.specs() if self._allowed(s.name)]

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        if not self._allowed(call.name):
            return f"error: tool '{call.name}' not allowed for this agent"
        return await self._inner.dispatch(call, context)


_SPAWN_SPEC = ToolSpec(
    name="spawn_agent",
    description=(
        "Run a named subagent on a task in its own sub-session and return "
        "its final answer."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}, "task": {"type": "string"}},
        "required": ["name", "task"],
    },
)


def register_spawn_tool(
    registry: ToolRegistry,
    agents: AgentRegistry,
    provider: Provider,
    default_model: str,
    budget: Budget | None = None,
) -> None:
    """Expose spawn_agent; sub-sessions reuse the shared registry, filtered."""

    async def spawn_agent(name: str, task: str) -> str:
        definition = agents.get(name)
        if definition is None:
            known = ", ".join(d.name for d in agents.scan()) or "none"
            return f"error: no agent named '{name}' (known: {known})"

        async def drop_delta(text: str) -> None:
            pass

        result = await run_turn(
            provider=provider,
            model=definition.model or default_model,
            messages=[
                {"role": "system", "content": definition.system_prompt},
                {"role": "user", "content": task},
            ],
            tools=FilteredTools(registry, definition.tools),
            on_delta=drop_delta,
        )
        if budget is not None:
            await budget.record(f"subagent:{name}", result.usage.cost)
        return result.text

    registry.register(Tool(_SPAWN_SPEC, spawn_agent))
