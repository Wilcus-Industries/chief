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


def make_gated(
    tmp_path: Path,
    answer: Approval,
    *,
    on_always: object = None,
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

    gated = GatedTools(
        registry=registry,
        policy=make_policy(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        context=CONTEXT,
        ask=ask,
        on_always=on_always,  # type: ignore[arg-type]
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


def test_approved_store_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "data" / "gate_approved.json"
    assert load_approved(path) == set()
    save_approved({"send", "self_edit"}, path)
    assert load_approved(path) == {"send", "self_edit"}
    # Stable, sorted on disk.
    assert json.loads(path.read_text()) == ["self_edit", "send"]
