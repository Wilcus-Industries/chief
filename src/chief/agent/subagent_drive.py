"""A subagent run's lifecycle loop (PRD #308).

A run that never messaged its parent auto-finishes when a turn ends, its last
text the result. Once it has, a turn ending means idle — it waits for the
parent's answer and finishes only by calling ``finish`` (or is killed by the
idle timeout). Spend is recorded per model call, so a cancel loses none, and a
spent budget downgrades the run's model or stops it.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from chief.agent.loop import PostTool, run_turn
from chief.agent.subagent_run import SubagentRun
from chief.budget import Budget, BudgetState
from chief.provider.base import Provider, Usage
from chief.tools import ToolDispatcher

logger = logging.getLogger(__name__)


@dataclass
class RunDeps:
    """What every run's loop shares: the model, the slot cap, the budget (and
    the model to fall back to once it is spent), and the post_tool screen (the
    core injection screen runs inside the subagent)."""

    provider: Provider
    default_model: str
    slots: asyncio.Semaphore
    budget: Budget | None = None
    post_tool: PostTool | None = None
    downgrade_model: str | None = None


# Hands a run's ending (result or kill notice) to the pool, for its parent.
Report = Callable[[SubagentRun, str], None]


async def _drop_delta(text: str) -> None:
    """Subagents stream to no one."""


async def drive(
    run: SubagentRun, tools: ToolDispatcher, deps: RunDeps,
    report: Report, idle_timeout: Callable[[], float],
) -> None:
    """Run turns until the run finishes, times out idle, fails, or is cancelled;
    every ending is reported to the parent (except a silent cancel).
    ``idle_timeout`` is read when the run goes idle, so a retune applies."""
    async def spent(usage: Usage) -> None:  # per call: a cancel loses nothing
        if deps.budget is not None:
            await deps.budget.record(f"subagent:{run.agent.name}", usage.cost)

    try:
        while True:
            model = await _model(run, deps)
            if model is None:
                return report(run, run.ended("stopped: budget exhausted"))
            async with deps.slots:  # held only while a turn runs, never idle
                run.running = True
                try:
                    result = await run_turn(
                        provider=deps.provider, model=model,
                        messages=run.messages, tools=tools, on_delta=_drop_delta,
                        post_tool=deps.post_tool, injections=run,
                        stop=lambda: run.finished_result is not None,
                        on_usage=spent,
                    )
                finally:
                    run.running = False
            if run.finished_result is not None:
                return report(run, run.finished_result)
            if not run.inbox and not run.messaged_parent:
                return report(run, result.text)
            timeout = idle_timeout()
            if not await _await_message(run, timeout):
                return report(run, run.ended(f"killed: idle {_span(timeout)}"))
            run.messages.extend(
                {"role": "user", "content": text} for text in await run.take()
            )
    except asyncio.CancelledError:
        if run.cancelled_by is not None:
            report(run, run.ended(f"cancelled by {run.cancelled_by}"))
        raise
    except Exception as exc:  # a dead provider must still reach the parent
        logger.exception("subagent %s failed", run.id)
        report(run, run.ended(f"failed: {exc}"))


async def _model(run: SubagentRun, deps: RunDeps) -> str | None:
    """The model for the run's next turn: its own, else the downgrade once the
    budget is spent — or None (stop) when no downgrade is configured."""
    status = await deps.budget.status() if deps.budget is not None else None
    if status is None or status.state is not BudgetState.EXHAUSTED:
        return run.agent.model or deps.default_model
    return deps.downgrade_model


def _span(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:g}s"


async def _await_message(run: SubagentRun, timeout: float) -> bool:
    """Wait (idle) for a message; False when the idle timeout passes first."""
    if run.inbox:
        return True
    run.wake.clear()
    try:
        await asyncio.wait_for(run.wake.wait(), timeout)
    except TimeoutError:
        return False
    return True
