"""The gate: list decisions, read-only auto-allow, cards, always-allow, audit."""

import json
from pathlib import Path

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.approvals import Approval
from chief.audit import AuditLog
from chief.gate import (
    Decision,
    GatedTools,
    GatePolicy,
    load_approved,
    save_approved,
)
from chief.provider.base import ToolCall, ToolSpec

CONTEXT = ToolContext(thread_key="cli:t", channel="cli")


def make_policy() -> GatePolicy:
    return GatePolicy(never=frozenset({"rm_rf"}), approved={"echo"})


def test_policy_decides_never_approved_ask() -> None:
    policy = make_policy()
    assert policy.decide("rm_rf") is Decision.NEVER
    assert policy.decide("echo") is Decision.APPROVED
    assert policy.decide("anything_else") is Decision.ASK


def test_allow_always_promotes_to_approved() -> None:
    policy = make_policy()
    assert policy.decide("gray") is Decision.ASK
    policy.allow_always("gray")
    assert policy.decide("gray") is Decision.APPROVED


def test_star_wildcard_approves_every_tool() -> None:
    policy = GatePolicy(never=frozenset({"rm_rf"}), approved={"*"})
    assert policy.decide("anything") is Decision.APPROVED
    assert policy.decide("write_file") is Decision.APPROVED
    # never still wins over the wildcard.
    assert policy.decide("rm_rf") is Decision.NEVER


def make_gated(
    tmp_path: Path,
    answer: Approval,
    *,
    on_always: object = None,
    announced: list[str] | None = None,
) -> tuple[GatedTools, list[str]]:
    registry = ToolRegistry()

    async def echo(text: str = "") -> str:
        return f"ran:{text}"

    specs = [
        ToolSpec(name="echo", description=".", parameters={}),
        ToolSpec(name="rm_rf", description=".", parameters={}),
        ToolSpec(name="gray", description=".", parameters={}),
        ToolSpec(name="peek", description=".", parameters={}, read_only=True),
    ]
    for spec in specs:
        registry.register(Tool(spec=spec, handler=echo))
    questions: list[str] = []

    async def ask(context: ToolContext, question: str) -> Approval:
        questions.append(question)
        return answer

    async def announce(context: ToolContext, text: str) -> None:
        assert announced is not None
        announced.append(text)

    gated = GatedTools(
        registry=registry,
        policy=make_policy(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        context=CONTEXT,
        ask=ask,
        on_always=on_always,  # type: ignore[arg-type]
        announce=announce if announced is not None else None,
    )
    return gated, questions


async def test_never_tool_is_denied_without_asking(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, Approval.ONCE)
    result = await gated.dispatch(ToolCall(id="1", name="rm_rf", arguments={}))
    assert result == "error: tool 'rm_rf' denied by the gate"
    assert questions == []


async def test_approved_tool_runs_without_asking(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, Approval.DENY)
    result = await gated.dispatch(
        ToolCall(id="1", name="echo", arguments={"text": "x"})
    )
    assert result == "ran:x"
    assert questions == []


async def test_read_only_tool_auto_approves(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, Approval.DENY)
    result = await gated.dispatch(ToolCall(id="1", name="peek", arguments={}))
    assert result == "ran:"
    assert questions == []


async def test_gray_tool_asks_and_runs_on_yes(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, Approval.ONCE)
    result = await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert result == "ran:"
    assert len(questions) == 1
    assert "gray" in questions[0]
    assert "always" in questions[0]


async def test_gray_tool_denied_on_no(tmp_path: Path) -> None:
    gated, _ = make_gated(tmp_path, Approval.DENY)
    result = await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert result == "error: tool 'gray' denied by the gate"


async def test_always_answer_runs_and_persists_tool(tmp_path: Path) -> None:
    promoted: list[str] = []
    gated, _ = make_gated(tmp_path, Approval.ALWAYS, on_always=promoted.append)
    result = await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert result == "ran:"
    assert promoted == ["gray"]


async def test_unknown_tool_errors_without_card(tmp_path: Path) -> None:
    """A phantom tool name never raises a card and never persists (audit C2)."""
    promoted: list[str] = []
    gated, questions = make_gated(
        tmp_path, Approval.ALWAYS, on_always=promoted.append
    )
    result = await gated.dispatch(
        ToolCall(id="1", name="phantom", arguments={"query": "x"})
    )
    assert result.startswith("error: unknown tool 'phantom'")
    # Self-correcting: names the real tools and the recovery path.
    assert "echo" in result
    assert "load_skill" in result
    assert questions == []
    assert promoted == []
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert entries[-1]["tool"] == "phantom"
    assert entries[-1]["outcome"] == "unknown_tool"


async def test_audit_records_each_outcome(tmp_path: Path) -> None:
    gated, _ = make_gated(tmp_path, Approval.ONCE)
    await gated.dispatch(ToolCall(id="1", name="echo", arguments={}))
    await gated.dispatch(ToolCall(id="2", name="rm_rf", arguments={}))
    await gated.dispatch(ToolCall(id="3", name="gray", arguments={}))
    await gated.dispatch(ToolCall(id="4", name="peek", arguments={}))
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    outcomes = {e["tool"]: e["outcome"] for e in entries}
    assert outcomes == {
        "echo": "list:approved",
        "rm_rf": "list:never",
        "gray": "card:once",
        "peek": "read_only",
    }


async def test_approved_call_is_announced(tmp_path: Path) -> None:
    """An always/approved tool is no longer silent — it announces itself."""
    announced: list[str] = []
    gated, questions = make_gated(tmp_path, Approval.DENY, announced=announced)
    await gated.dispatch(ToolCall(id="1", name="echo", arguments={"text": "x"}))
    assert questions == []
    assert announced == ['⚙ echo {"text": "x"}']


async def test_read_only_and_denied_calls_are_announced(tmp_path: Path) -> None:
    announced: list[str] = []
    gated, _ = make_gated(tmp_path, Approval.DENY, announced=announced)
    await gated.dispatch(ToolCall(id="1", name="peek", arguments={}))
    await gated.dispatch(ToolCall(id="2", name="rm_rf", arguments={}))
    assert announced == ["⚙ peek {}", "⚙ rm_rf {} — denied by the gate"]


async def test_card_call_is_not_double_announced(tmp_path: Path) -> None:
    """The card already shows the call; a notice would duplicate it."""
    announced: list[str] = []
    gated, questions = make_gated(tmp_path, Approval.ONCE, announced=announced)
    await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert len(questions) == 1
    assert announced == []


async def test_unknown_tool_is_not_announced(tmp_path: Path) -> None:
    announced: list[str] = []
    gated, _ = make_gated(tmp_path, Approval.ONCE, announced=announced)
    await gated.dispatch(ToolCall(id="1", name="phantom", arguments={}))
    assert announced == []


async def test_long_arguments_are_truncated(tmp_path: Path) -> None:
    announced: list[str] = []
    gated, _ = make_gated(tmp_path, Approval.DENY, announced=announced)
    await gated.dispatch(
        ToolCall(id="1", name="echo", arguments={"text": "x" * 500})
    )
    assert len(announced[0]) < 200
    assert announced[0].endswith("…")


async def test_announce_failure_does_not_break_the_call(tmp_path: Path) -> None:
    """A dead channel must not turn a working tool call into an error."""
    registry = ToolRegistry()

    async def echo(text: str = "") -> str:
        return f"ran:{text}"

    registry.register(
        Tool(spec=ToolSpec(name="echo", description=".", parameters={}), handler=echo)
    )

    async def ask(context: ToolContext, question: str) -> Approval:
        return Approval.DENY

    async def announce(context: ToolContext, text: str) -> None:
        raise RuntimeError("channel is gone")

    gated = GatedTools(
        registry=registry,
        policy=make_policy(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        context=CONTEXT,
        ask=ask,
        announce=announce,
    )
    assert await gated.dispatch(ToolCall(id="1", name="echo", arguments={})) == "ran:"


def test_approved_store_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "data" / "gate_approved.json"
    assert load_approved(path) == set()
    save_approved({"send", "self_edit"}, path)
    assert load_approved(path) == {"send", "self_edit"}
    # Stable, sorted on disk.
    assert json.loads(path.read_text()) == ["self_edit", "send"]
