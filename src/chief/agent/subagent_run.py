"""One detached subagent: its state, its toolset, and its lifecycle loop.

Lifecycle (PRD #308): a run that never messaged its parent auto-finishes when
a turn ends, its last text the result. Once it has, a turn ending means idle —
it waits for the parent's answer and finishes only by calling ``finish`` (or
is killed by the idle timeout). Subagents are ephemeral: no thread row, no
persisted transcript, not resumable across a restart.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from chief.agent.loop import PostTool, run_turn
from chief.budget import Budget
from chief.provider.base import Provider, ToolCall, ToolSpec
from chief.subagents import AgentDef
from chief.tools import ToolContext, ToolDispatcher

logger = logging.getLogger(__name__)

PROGRESS_CHARS = 500

FINISH_SPEC = ToolSpec(
    name="finish",
    description=(
        "End this subagent run and hand `result` to your parent. Required once "
        "you have messaged your parent; before that, your final reply is the "
        "result on its own."
    ),
    parameters={
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    },
)


@dataclass
class SubagentRun:
    """A live subagent: addressable as ``id`` while it runs."""

    id: str
    parent_key: str
    parent_channel: str
    agent: AgentDef
    task: str
    messages: list[dict[str, Any]]
    started: float = field(default_factory=time.monotonic)
    inbox: list[str] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False
    messaged_parent: bool = False
    finished_result: str | None = None
    last_text: str = ""
    # Set while a ``spawn_agent(wait=true)`` caller is blocked on this run.
    waiter: "asyncio.Future[str] | None" = None
    cancelled_by: str | None = None  # None on a silent cancel (orphan cleanup)

    def take(self) -> list[dict[str, Any]]:
        """Drain the inbox as user messages (the loop's ``drain`` hook)."""
        taken, self.inbox = self.inbox, []
        return [{"role": "user", "content": text} for text in taken]

    def progress(self) -> str:
        """The last assistant text, cut short — the 'partial progress' in a
        kill or cancel notice."""
        text = self.last_text or "(none yet)"
        return text if len(text) <= PROGRESS_CHARS else text[:PROGRESS_CHARS] + "…"

    def ended(self, how: str) -> str:
        """A kill/cancel notice body: what happened, the task, the progress."""
        return f"{how}.\ntask: {self.task}\nlast progress: {self.progress()}"


class RunTools:
    """The run's dispatcher: its filtered, gated tools plus ``finish``, which
    never reaches the gate — it only marks the run done."""

    def __init__(self, inner: ToolDispatcher, run: SubagentRun) -> None:
        self._inner = inner
        self._run = run

    def specs(self) -> list[ToolSpec]:
        return [*self._inner.specs(), FINISH_SPEC]

    async def dispatch(self, call: ToolCall, context: ToolContext | None = None) -> str:
        if call.name == FINISH_SPEC.name:
            self._run.finished_result = str(call.arguments.get("result", ""))
            return "finished"
        return await self._inner.dispatch(call, context)


@dataclass
class RunDeps:
    """What every run's loop shares: the model, the slot cap, the budget, and
    the post_tool screen (the core injection screen runs inside the subagent)."""

    provider: Provider
    default_model: str
    slots: asyncio.Semaphore
    budget: Budget | None = None
    post_tool: PostTool | None = None


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
    try:
        while True:
            async with deps.slots:  # held only while a turn runs, never idle
                run.running = True
                try:
                    result = await run_turn(
                        provider=deps.provider,
                        model=run.agent.model or deps.default_model,
                        messages=run.messages, tools=tools, on_delta=_drop_delta,
                        post_tool=deps.post_tool, drain=run.take,
                        stop=lambda: run.finished_result is not None,
                    )
                finally:
                    run.running = False
            run.last_text = result.text or run.last_text
            if deps.budget is not None:
                cost = result.usage.cost
                await deps.budget.record(f"subagent:{run.agent.name}", cost)
            if run.finished_result is not None:
                return report(run, run.finished_result)
            if not run.inbox and not run.messaged_parent:
                return report(run, result.text)
            timeout = idle_timeout()
            if not await _await_message(run, timeout):
                return report(run, run.ended(f"killed: idle {_span(timeout)}"))
            run.messages.extend(run.take())
    except asyncio.CancelledError:
        if run.cancelled_by is not None:
            report(run, run.ended(f"cancelled by {run.cancelled_by}"))
        raise
    except Exception as exc:  # a dead provider must still reach the parent
        logger.exception("subagent %s failed", run.id)
        report(run, run.ended(f"failed: {exc}"))


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
