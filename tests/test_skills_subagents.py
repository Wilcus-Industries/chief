"""Skills (SKILL.md, progressive disclosure, /skill invocation) and subagents."""

from pathlib import Path

from chief.adapters.base import Message
from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import Completion, ToolCall, ToolSpec
from chief.skills import SkillLibrary, register_skill_tools
from chief.subagents import AgentRegistry, FilteredTools, register_spawn_tool

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


async def test_spawn_agent_runs_a_sub_session(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="echo", arguments={"text": "notes"})
    provider = FakeProvider(
        [[Completion(text="", tool_calls=(call,))], text_turn("summary: notes")]
    )
    registry = echo_registry()
    register_spawn_tool(registry, make_agents(tmp_path), provider, "default-model")
    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="spawn_agent",
            arguments={"name": "researcher", "task": "summarize my notes"},
        )
    )
    assert result == "summary: notes"
    system = provider.calls[0][0]
    assert system["content"].startswith("You are a focused researcher.")
    # The subagent never sees spawn_agent (no recursion).
    unknown = await registry.dispatch(
        ToolCall(id="2", name="spawn_agent", arguments={"name": "ghost", "task": "x"})
    )
    assert unknown.startswith("error: no agent named 'ghost'")
