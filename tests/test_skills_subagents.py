"""Skills (SKILL.md, progressive disclosure, /skill invocation) and subagents."""

import asyncio
import json
from pathlib import Path

from chief.adapters.base import Message
from chief.approvals import Approval, ApprovalBroker
from chief.audit import AuditLog
from chief.gate import GatedTools, GatePolicy
from chief.provider.base import Completion, ProviderEvent, ToolCall, ToolSpec
from chief.skills import SkillLibrary, register_skill_tools
from chief.subagents import AgentRegistry, FilteredTools, register_spawn_tool
from chief.tools import Tool, ToolContext, ToolDispatcher, ToolRegistry

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
    async def echo(text: str = "") -> str:
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
    registry.register(
        Tool(ToolSpec(name="rm_rf", description=".", parameters={}), hidden)
    )
    registry.register(
        Tool(
            ToolSpec(name="peek", description=".", parameters={}, read_only=True),
            hidden,
        )
    )
    return registry


def test_agent_registry_parses_frontmatter(tmp_path: Path) -> None:
    agents = make_agents(tmp_path)
    definition = agents.get("researcher")
    assert definition is not None
    assert definition.tools == ("echo",)
    assert definition.system_prompt.startswith("You are a focused researcher.")


def test_a_definition_without_a_tools_key_gets_no_tools(tmp_path: Path) -> None:
    # Default-closed: an omitted `tools:` grants nothing, so a definition
    # written without thinking about tools cannot reach the whole registry.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "vague.md").write_text(
        "---\nname: vague\ndescription: d\n---\nBe terse.\n"
    )
    definition = AgentRegistry(agents_dir).get("vague")
    assert definition is not None
    assert definition.tools == ()


async def test_filtered_tools_hides_and_blocks(tmp_path: Path) -> None:
    filtered = FilteredTools(echo_registry(), ("echo",))
    assert [s.name for s in filtered.specs()] == ["echo"]
    blocked = await filtered.dispatch(ToolCall(id="1", name="hidden", arguments={}))
    assert blocked == "error: tool 'hidden' not allowed for this agent"


async def test_filtered_tools_with_an_empty_allowlist_blocks_everything() -> None:
    filtered = FilteredTools(echo_registry(), ())
    assert filtered.specs() == []
    blocked = await filtered.dispatch(ToolCall(id="1", name="echo", arguments={}))
    assert blocked == "error: tool 'echo' not allowed for this agent"


PARENT = ToolContext(thread_key="cli:t", channel="cli")

SUB_AGENT_MD = """---
name: {name}
description: d
tools: [{tools}]
---
Be terse.
"""


def make_sub_agents(tmp_path: Path) -> AgentRegistry:
    """One definition per tool under test, so each names its own allowlist."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    for name, tools in (
        ("researcher", "echo"),
        ("destroyer", "rm_rf"),
        ("reader", "peek"),
    ):
        (agents_dir / f"{name}.md").write_text(
            SUB_AGENT_MD.format(name=name, tools=tools)
        )
    return AgentRegistry(agents_dir)


class GateEnv:
    """A parent GatedTools whose spawn_agent gates its sub-session the same way."""

    def __init__(
        self,
        tmp_path: Path,
        provider: FakeProvider,
        answer: Approval,
        ask_override: object = None,
    ):
        self.questions: list[str] = []
        self.announced: list[str] = []
        self.audit_path = tmp_path / "audit.jsonl"
        self.always: list[str] = []
        audit = AuditLog(self.audit_path)
        # spawn_agent is pre-approved so the only card a test sees is the
        # subagent's own tool call; rm_rf is the `never` entry.
        policy = GatePolicy(never=frozenset({"rm_rf"}), approved={"spawn_agent"})

        async def default_ask(context: ToolContext, question: str) -> Approval:
            self.questions.append(question)
            return answer

        ask = ask_override or default_ask

        async def announce(context: ToolContext, text: str) -> None:
            self.announced.append(text)

        def gated(context: ToolContext, inner: ToolDispatcher) -> ToolDispatcher:
            return GatedTools(
                registry=inner, policy=policy, audit=audit, context=context,
                ask=ask,  # type: ignore[arg-type]
                on_always=self.always.append, announce=announce,
            )

        self.registry = echo_registry()
        register_spawn_tool(
            self.registry, make_sub_agents(tmp_path), provider, "default-model",
            gated=gated,
        )
        self.parent = gated(PARENT, self.registry)

    async def spawn(self, name: str, task: str = "go") -> str:
        return await self.parent.dispatch(
            ToolCall(id="1", name="spawn_agent", arguments={"name": name, "task": task})
        )

    def rows(self) -> list[dict[str, object]]:
        text = self.audit_path.read_text()
        return [json.loads(line) for line in text.splitlines() if line]


def sub_tool_results(provider: FakeProvider) -> list[str]:
    """The tool results the sub-session fed back to the model on its last call."""
    return [m["content"] for m in provider.calls[-1] if m.get("role") == "tool"]


def one_call_then(text: str, call: ToolCall) -> list[list[ProviderEvent]]:
    """Script one sub-session: a single tool call, then a final text reply."""
    first: list[ProviderEvent] = [Completion(text="", tool_calls=(call,))]
    return [first, text_turn(text)]


ECHO_CALL = ToolCall(id="c1", name="echo", arguments={"text": "notes"})


async def test_spawn_agent_runs_a_sub_session(tmp_path: Path) -> None:
    provider = FakeProvider(one_call_then("summary: notes", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    assert await env.spawn("researcher", "summarize my notes") == "summary: notes"
    system = provider.calls[0][0]
    assert system["content"].startswith("Be terse.")
    # The subagent never sees spawn_agent (no recursion).
    assert "spawn_agent" not in [s.name for s in provider.tool_specs[0]]
    unknown = await env.spawn("ghost")
    assert unknown.startswith("error: no agent named 'ghost'")


async def test_subagent_tool_call_raises_a_card_on_the_parent_thread(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(one_call_then("done", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    await env.spawn("researcher")
    assert len(env.questions) == 1
    # The owner must be told which agent is asking — they did not initiate it.
    assert env.questions[0].startswith("subagent 'researcher': approve tool call echo")
    assert sub_tool_results(provider) == ["echo: notes"]


async def test_denying_a_subagent_card_denies_the_call(tmp_path: Path) -> None:
    provider = FakeProvider(one_call_then("done", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.DENY)
    await env.spawn("researcher")
    assert sub_tool_results(provider) == ["error: tool 'echo' denied by the gate"]


async def test_never_denies_a_subagent_call(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="rm_rf", arguments={})
    provider = FakeProvider(one_call_then("done", call))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    await env.spawn("destroyer")
    assert sub_tool_results(provider) == ["error: tool 'rm_rf' denied by the gate"]
    assert env.questions == []  # never never asks


async def test_read_only_still_auto_approves_for_a_subagent(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="peek", arguments={})
    provider = FakeProvider(one_call_then("done", call))
    env = GateEnv(tmp_path, provider, Approval.DENY)
    await env.spawn("reader")
    assert sub_tool_results(provider) == ["secret"]
    assert env.questions == []


async def test_a_tool_outside_the_allowlist_errors_without_a_card(
    tmp_path: Path,
) -> None:
    # Gate outermost, filter inside: GatedTools.specs() sees the filtered set,
    # so a disallowed tool takes the unknown-tool branch — no card, no always.
    call = ToolCall(id="c1", name="hidden", arguments={})
    provider = FakeProvider(one_call_then("done", call))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    await env.spawn("researcher")
    assert sub_tool_results(provider) == [
        "error: tool 'hidden' not allowed for this agent"
    ]
    assert env.questions == []
    assert env.always == []


async def test_subagent_calls_are_audited_against_the_calling_agent(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(one_call_then("done", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    await env.spawn("researcher")
    echo_rows = [r for r in env.rows() if r["tool"] == "echo"]
    assert len(echo_rows) == 1
    assert echo_rows[0]["agent"] == "researcher"
    assert echo_rows[0]["thread"] == "cli:t"  # the parent's surface
    # The parent's own spawn_agent call is attributed to no agent.
    spawn_rows = [r for r in env.rows() if r["tool"] == "spawn_agent"]
    assert spawn_rows[0]["agent"] is None


async def test_always_from_a_subagent_persists_globally(tmp_path: Path) -> None:
    # The owner's call: same switch as anywhere, no strange exceptions.
    provider = FakeProvider(one_call_then("done", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.ALWAYS)
    await env.spawn("researcher")
    assert env.always == ["echo"]


async def test_a_parent_blocked_on_a_subagent_card_still_resolves(
    tmp_path: Path,
) -> None:
    # The deadlock that isn't: the parent is blocked inside spawn_agent while
    # its subagent awaits a card on the parent's own thread. The broker takes
    # the answer without needing the parent's turn to finish.
    broker = ApprovalBroker()
    provider = FakeProvider(one_call_then("done", ECHO_CALL))

    async def send(question: str) -> None:
        asyncio.get_running_loop().call_soon(broker.resolve, "cli:t", "yes")

    async def ask(context: ToolContext, question: str) -> Approval:
        return await broker.ask(context.thread_key, question, send)

    env = GateEnv(tmp_path, provider, Approval.DENY, ask_override=ask)
    result = await asyncio.wait_for(env.spawn("researcher"), timeout=5)
    assert result == "done"
    assert sub_tool_results(provider) == ["echo: notes"]


async def test_spawn_agent_without_a_context_fails_closed(tmp_path: Path) -> None:
    # Dispatched off a session there is no surface to raise a card on, so the
    # sub-session must not run ungated.
    provider = FakeProvider(one_call_then("done", ECHO_CALL))
    env = GateEnv(tmp_path, provider, Approval.ONCE)
    result = await env.registry.dispatch(
        ToolCall(
            id="1", name="spawn_agent", arguments={"name": "researcher", "task": "x"}
        )
    )
    assert result.startswith("error: spawn_agent needs a session context")
