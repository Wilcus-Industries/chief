"""One detached subagent: its state and its toolset (``finish`` included).

Subagents are ephemeral: no thread row, no persisted transcript, not
resumable across a restart. The lifecycle loop is ``subagent_drive.drive``.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from chief.provider.base import ToolCall, ToolSpec
from chief.subagents import AgentDef
from chief.tools import ToolContext, ToolDispatcher

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
    # Set while a ``spawn_agent(wait=true)`` caller is blocked on this run.
    waiter: "asyncio.Future[str] | None" = None
    cancelled_by: str | None = None  # None on a silent cancel (orphan cleanup)

    def pending(self) -> bool:
        """The loop's ``Injections``: a message is waiting for this run."""
        return bool(self.inbox)

    async def take(self) -> list[str]:
        """Drain the inbox (the loop appends each as a user message)."""
        taken, self.inbox = self.inbox, []
        return taken

    def progress(self) -> str:
        """The last assistant text so far, cut short — the 'partial progress'
        in a kill or cancel notice. Read off the transcript, which the loop
        grows in place, so a run killed mid-turn reports where it got to."""
        said = (m["content"] for m in reversed(self.messages)
                if m["role"] == "assistant" and m.get("content"))
        text = next(said, "(none yet)")
        return text if len(text) <= PROGRESS_CHARS else text[:PROGRESS_CHARS] + "…"

    def ended(self, how: str) -> str:
        """A kill/cancel notice body: what happened, the task, the progress."""
        return f"{how}.\ntask: {self.task}\nlast progress: {self.progress()}"


TASK_PREVIEW_CHARS = 60


def describe(run: SubagentRun, now: float) -> str:
    """One ``/tasks`` / ``action=list`` line for a live run."""
    state = "running" if run.running else "idle"
    task = run.task
    if len(task) > TASK_PREVIEW_CHARS:
        task = task[:TASK_PREVIEW_CHARS] + "…"
    age = int(now - run.started)
    parent = run.parent_key
    return f"{run.id} [{run.agent.name}] parent={parent} {state} {age}s — {task}"


def describe_all(runs: list[SubagentRun]) -> str:
    """Every line, or the empty-list answer."""
    now = time.monotonic()
    return "\n".join(describe(r, now) for r in runs) or "no subagents running"


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
