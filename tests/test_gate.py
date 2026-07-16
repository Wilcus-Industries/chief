"""The gate: list decisions, approval cards, audit trail."""

import json
from pathlib import Path

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.audit import AuditLog
from chief.gate import Decision, GatedTools, GatePolicy
from chief.provider.base import ToolCall, ToolSpec

POLICY = GatePolicy(never=frozenset({"rm_rf"}), approved=frozenset({"echo"}))
CONTEXT = ToolContext(thread_key="cli:t", channel="cli")


def test_policy_decides_never_approved_ask() -> None:
    assert POLICY.decide("rm_rf") is Decision.NEVER
    assert POLICY.decide("echo") is Decision.APPROVED
    assert POLICY.decide("anything_else") is Decision.ASK


def make_gated(tmp_path: Path, answer: bool) -> tuple[GatedTools, list[str]]:
    registry = ToolRegistry()

    async def echo(text: str = "") -> str:
        return f"ran:{text}"

    for name in ("echo", "rm_rf", "gray"):
        registry.register(
            Tool(
                spec=ToolSpec(name=name, description=".", parameters={}),
                handler=echo,
            )
        )
    questions: list[str] = []

    async def ask(context: ToolContext, question: str) -> bool:
        questions.append(question)
        return answer

    gated = GatedTools(
        registry=registry,
        policy=POLICY,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        context=CONTEXT,
        ask=ask,
    )
    return gated, questions


async def test_never_tool_is_denied_without_asking(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, answer=True)
    result = await gated.dispatch(ToolCall(id="1", name="rm_rf", arguments={}))
    assert result == "error: tool 'rm_rf' denied by the gate"
    assert questions == []


async def test_approved_tool_runs_without_asking(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, answer=False)
    result = await gated.dispatch(
        ToolCall(id="1", name="echo", arguments={"text": "x"})
    )
    assert result == "ran:x"
    assert questions == []


async def test_gray_tool_asks_and_runs_on_yes(tmp_path: Path) -> None:
    gated, questions = make_gated(tmp_path, answer=True)
    result = await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert result == "ran:"
    assert len(questions) == 1
    assert "gray" in questions[0]


async def test_gray_tool_denied_on_no(tmp_path: Path) -> None:
    gated, _ = make_gated(tmp_path, answer=False)
    result = await gated.dispatch(ToolCall(id="1", name="gray", arguments={}))
    assert result == "error: tool 'gray' denied by the gate"


async def test_every_call_lands_in_the_audit_log(tmp_path: Path) -> None:
    gated, _ = make_gated(tmp_path, answer=True)
    await gated.dispatch(ToolCall(id="1", name="echo", arguments={}))
    await gated.dispatch(ToolCall(id="2", name="rm_rf", arguments={}))
    await gated.dispatch(ToolCall(id="3", name="gray", arguments={}))
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    outcomes = {e["tool"]: e["outcome"] for e in entries}
    assert outcomes == {
        "echo": "list:approved",
        "rm_rf": "list:never",
        "gray": "card:approved",
    }
    assert all(e["thread"] == "cli:t" for e in entries)
