"""One subagent run's lifecycle loop (``drive``) in isolation: what a kill or
cancel notice reports, and what the run spends against the budget."""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.agent.subagent_drive import RunDeps, drive
from chief.agent.subagent_run import RunTools, SubagentRun
from chief.budget import Budget
from chief.persistence.db import make_session_factory
from chief.provider.base import Completion, ToolCall, ToolSpec, Usage
from chief.subagents import AgentDef
from chief.tools import Tool, ToolRegistry

from .fakes import FakeProvider, text_turn

AGENT = AgentDef(name="researcher", description=".", system_prompt="Go.",
                 tools=None, model=None)


def make_run(messages: list[dict[str, Any]] | None = None) -> SubagentRun:
    return SubagentRun(
        id="sub:cli:a:abc123", parent_key="cli:a", parent_channel="cli",
        agent=AGENT, task="dig",
        messages=messages or [{"role": "user", "content": "dig"}],
    )


def costed_call(cost: float) -> list[Any]:
    call = ToolCall(id="c1", name="slow", arguments={})
    return [Completion(text="looked at 3 of 5 files", tool_calls=(call,),
                       usage=Usage(cost=cost))]


def blocking_tools(release: asyncio.Event) -> ToolRegistry:
    async def slow() -> str:
        await release.wait()
        return "done"

    registry = ToolRegistry()
    registry.register(Tool(ToolSpec(name="slow", description=".", parameters={}),
                           slow))
    return registry


def test_progress_is_the_last_assistant_text_even_mid_turn() -> None:
    run = make_run()
    assert run.progress() == "(none yet)"
    run.messages.append({"role": "assistant", "content": "looked at 3 of 5"})
    run.messages.append({"role": "tool", "tool_call_id": "c1", "content": "x"})
    run.messages.append({"role": "assistant", "content": None})
    assert run.progress() == "looked at 3 of 5"


async def test_a_cancelled_turn_still_records_its_spend_and_progress(
    engine: AsyncEngine,
) -> None:
    budget = Budget(make_session_factory(engine), cap_usd=0)
    run = make_run()
    run.cancelled_by = "owner"
    reports: list[str] = []
    provider = FakeProvider([costed_call(0.5)])
    deps = RunDeps(provider, "m", asyncio.Semaphore(1), budget=budget)
    tools = RunTools(blocking_tools(asyncio.Event()), run)
    job = asyncio.create_task(
        drive(run, tools, deps, lambda _r, text: reports.append(text), lambda: 60)
    )
    while not run.messages[-1].get("tool_calls"):  # parked in the slow tool
        await asyncio.sleep(0)
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)
    assert (await budget.status()).spent == 0.5
    [notice] = reports
    assert notice.startswith("cancelled by owner")
    assert "last progress: looked at 3 of 5 files" in notice


async def test_an_exhausted_budget_stops_a_run_or_downgrades_it(
    engine: AsyncEngine,
) -> None:
    budget = Budget(make_session_factory(engine), cap_usd=1.0)
    await budget.record("cli:a", 1.0)
    reports: list[str] = []
    provider = FakeProvider([])
    run = make_run()
    deps = RunDeps(provider, "m", asyncio.Semaphore(1), budget=budget)
    await drive(run, RunTools(ToolRegistry(), run), deps,
                lambda _r, text: reports.append(text), lambda: 60)
    assert provider.calls == []
    assert reports[0].startswith("stopped: budget exhausted")

    provider = FakeProvider([text_turn("cheap answer")])
    run = make_run()
    deps = RunDeps(provider, "m", asyncio.Semaphore(1), budget=budget,
                   downgrade_model="cheap")
    await drive(run, RunTools(ToolRegistry(), run), deps,
                lambda _r, text: reports.append(text), lambda: 60)
    assert provider.models == ["cheap"]
    assert reports[1] == "cheap answer"
