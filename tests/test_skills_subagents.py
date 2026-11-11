"""Skills (SKILL.md, progressive disclosure, /skill invocation) and subagents."""

import asyncio
from pathlib import Path

from chief.adapters.base import Message
from chief.agent.loop import PostTool
from chief.agent.subagent_pool import SubagentPool
from chief.agent.subagent_run import RunDeps
from chief.audit import AuditLog
from chief.provider.base import Completion, ToolCall, ToolSpec
from chief.skills import SkillLibrary, register_skill_tools
from chief.subagents import (
    AgentRegistry,
    FilteredTools,
    SubagentScope,
    register_spawn_tool,
)
from chief.tools import Tool, ToolContext, ToolRegistry

from .fakes import FakeProvider, text_turn

SKILL_MD = """---
name: greet
description: Greet someone warmly.
---
# Greeting skill

Say hello enthusiastically.
"""

AGENT_MD = """---
name: researcher
description: Reads things and summarizes.
tools: [echo]
---
You are a focused researcher. Answer tersely.
"""


def make_skills(tmp_path: Path) -> SkillLibrary:
    skill_dir = tmp_path / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    return SkillLibrary(tmp_path / "skills")


def test_library_scans_and_indexes(tmp_path: Path) -> None:
    library = make_skills(tmp_path)
    skills = library.scan()
    assert [s.name for s in skills] == ["greet"]
    assert skills[0].description == "Greet someone warmly."
    lines = library.prompt_lines()
    assert "- greet: Greet someone warmly." in lines
    assert "load_skill" in lines


def test_empty_library_adds_nothing_to_the_prompt(tmp_path: Path) -> None:
    assert SkillLibrary(tmp_path / "nowhere").prompt_lines() == ""


def test_library_scans_a_lowercase_skill_file(tmp_path: Path) -> None:
    # SKILL.md is the convention, but the loader must not silently drop a
    # stray skill.md: the glob is case-sensitive on Linux, so a mis-cased
    # file that works on a Mac would vanish in production otherwise.
    skill_dir = tmp_path / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(SKILL_MD)
    assert [s.name for s in SkillLibrary(tmp_path / "skills").scan()] == ["greet"]


def test_library_ignores_a_non_skill_markdown_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "README.md").write_text(SKILL_MD)
    assert SkillLibrary(tmp_path / "skills").scan() == []


async def test_load_skill_tool_returns_the_body(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_skill_tools(registry, make_skills(tmp_path))
    body = await registry.dispatch(
        ToolCall(id="1", name="load_skill", arguments={"name": "greet"})
    )
    assert "Say hello enthusiastically." in body
    missing = await registry.dispatch(
        ToolCall(id="2", name="load_skill", arguments={"name": "nope"})
    )
    assert missing == "error: no skill named 'nope'"


async def test_slash_skill_rewrites_the_turn(tmp_path: Path) -> None:
    from chief.commands import CommandSet

    library = make_skills(tmp_path)
    commands = CommandSet(None, None, None, None, skills=library)  # type: ignore[arg-type]
    message = Message(
        channel="cli", sender="owner", thread_key="cli:t", text="/greet Alice"
    )
    outcome = await commands.run(message)
    assert isinstance(outcome, Message)
    assert "Say hello enthusiastically." in outcome.text
    assert "Arguments: Alice" in outcome.text
    assert outcome.thread_key == "cli:t"


def make_agents(tmp_path: Path) -> AgentRegistry:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "researcher.md").write_text(AGENT_MD)
    return AgentRegistry(agents_dir)


def echo_registry() -> ToolRegistry:
    async def echo(text: str) -> str:
        return f"echo: {text}"

    async def hidden() -> str:
        return "secret"

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolSpec(
                name="echo",
                description=".",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            ),
            echo,
        )
    )
    registry.register(
        Tool(ToolSpec(name="hidden", description=".", parameters={}), hidden)
    )
    return registry


def test_agent_registry_parses_frontmatter(tmp_path: Path) -> None:
    agents = make_agents(tmp_path)
    definition = agents.get("researcher")
    assert definition is not None
    assert definition.tools == ("echo",)
    assert definition.system_prompt.startswith("You are a focused researcher.")


async def test_filtered_tools_hides_and_blocks(tmp_path: Path) -> None:
    filtered = FilteredTools(echo_registry(), ("echo",))
    assert [s.name for s in filtered.specs()] == ["echo"]
    blocked = await filtered.dispatch(ToolCall(id="1", name="hidden", arguments={}))
    assert blocked == "error: tool 'hidden' not allowed for this agent"


def spawn_registry(
    tmp_path: Path, provider: FakeProvider, post_tool: PostTool | None = None
) -> ToolRegistry:
    """A registry with spawn_agent over a real pool whose runs get the echo
    tools (ungated here; the gated path is covered in test_subagents_async)."""
    registry = echo_registry()
    deps = RunDeps(provider, "default-model", asyncio.Semaphore(2),
                   post_tool=post_tool)
    pool = SubagentPool(
        deps, lambda parent, channel, run: registry,
        lambda to, channel, text: None, AuditLog(tmp_path / "audit.jsonl"), 60.0,
    )
    register_spawn_tool(registry, make_agents(tmp_path), pool)
    return registry


CONTEXT = ToolContext("cli:parent", "cli")


async def test_filtered_tools_always_lets_a_subagent_reach_its_parent() -> None:
    registry = echo_registry()
    registry.register(
        Tool(ToolSpec(name="send_msg", description=".", parameters={}), _ok)
    )
    filtered = FilteredTools(registry, ("echo",), subagent=True)
    assert [s.name for s in filtered.specs()] == ["echo", "send_msg"]
    assert [s.name for s in FilteredTools(registry, ("echo",)).specs()] == ["echo"]


async def _ok() -> str:
    return "ok"


async def test_a_subagent_can_never_restart_or_revert_the_daemon() -> None:
    # A detached run has no turn boundary to fire the restart at: request()
    # would close turn admission and nothing would ever exec — a wedge.
    registry = echo_registry()
    for name in ("restart", "revert_edits"):
        registry.register(Tool(ToolSpec(name=name, description=".", parameters={}),
                               _ok))
    filtered = FilteredTools(registry, None, subagent=True)
    assert [s.name for s in filtered.specs()] == ["echo", "hidden"]
    refused = await filtered.dispatch(ToolCall(id="1", name="restart", arguments={}))
    assert refused == "error: tool 'restart' not allowed for this agent"
    allowlisted = FilteredTools(registry, ("restart",), subagent=True)
    assert allowlisted.specs() == []


async def test_subagent_scope_hands_the_run_context_to_mcp_and_send_msg_only() -> None:
    async def whoami(context: ToolContext | None = None) -> str:
        return context.thread_key if context is not None else "none"

    registry = ToolRegistry()
    for name in ("mcp_srv_whoami", "send_msg", "native_whoami"):
        registry.register(Tool(ToolSpec(name=name, description=".", parameters={}),
                               whoami, wants_context=True))
    scope = SubagentScope(registry)
    run = ToolContext("+15551234567", "imessage", subagent="sub:+15551234567:abc")
    seen = [await scope.dispatch(ToolCall(id="1", name=n, arguments={}), run)
            for n in ("mcp_srv_whoami", "send_msg", "native_whoami")]
    assert seen == ["+15551234567", "+15551234567", "none"]


async def test_spawn_agent_screens_sub_session_tool_results(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "notes"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("summary")]
    )
    screened: list[str] = []

    async def post_tool(tool_call: ToolCall, result: str) -> str:
        screened.append(tool_call.name)
        return f"[screened] {result}"

    registry = spawn_registry(tmp_path, provider, post_tool)
    await registry.dispatch(ToolCall(
        id="1", name="spawn_agent",
        arguments={"name": "researcher", "task": "t", "wait": True}), CONTEXT)
    assert screened == ["echo"]
    assert provider.calls[1][-1]["content"].startswith("[screened]")


async def test_spawn_agent_wait_returns_the_final_answer(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "notes"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("summary: notes")]
    )
    registry = spawn_registry(tmp_path, provider)
    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="spawn_agent",
            arguments={"name": "researcher", "task": "summarize my notes",
                       "wait": True},
        ),
        CONTEXT,
    )
    assert result == "summary: notes"
    system = provider.calls[0][0]
    assert "You are a focused researcher." in system["content"]
    # The subagent never sees spawn_agent (no recursion), but gets finish.
    names = [spec.name for spec in provider.tool_specs[0]]
    assert "spawn_agent" not in names and "finish" in names
    unknown = await registry.dispatch(
        ToolCall(id="2", name="spawn_agent", arguments={"name": "ghost", "task": "x"}),
        CONTEXT,
    )
    assert unknown.startswith("error: no agent named 'ghost' (known: researcher)")
