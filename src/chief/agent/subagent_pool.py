"""SubagentPool: the live subagent runs — spawn, message, list, cancel.

Every subagent→parent message (a question, the result, a kill, a timeout)
goes through :meth:`report_to_parent`, so a parent blocked in
``spawn_agent(wait=true)`` is interrupted by the first of them and every other
one arrives as an ordinary peer message (injected or waking the parent).
Subagent turns take their own semaphore, apart from the session cap, so a
fan-out can't starve the owner's threads.
"""

import asyncio
import secrets
from collections.abc import Callable
from typing import Any

from chief.agent.subagent_run import (
    RunDeps,
    RunTools,
    SubagentRun,
    describe_all,
    drive,
)
from chief.audit import AuditLog
from chief.peers.envelope import PeerMessage, new_id, render, subagent_label
from chief.subagents import PREAMBLE, AgentDef, FilteredTools
from chief.tools import ToolDispatcher

# (parent thread, parent channel, run id) -> the run's gated tool dispatcher.
SubagentToolsFactory = Callable[[str, str, str], ToolDispatcher]
# (to, channel, rendered text): inject into the parent or wake it.
DeliverToSession = Callable[[str, str, str], None]


def new_run_id(parent_key: str) -> str:
    """A subagent address, ``sub:<parent>:<6 hex>``."""
    return f"sub:{parent_key}:{secrets.token_hex(3)}"


def interrupt_text(run_id: str, question: str) -> str:
    """What a ``wait=true`` spawn returns when its subagent asks something."""
    return (
        f"subagent {run_id} asks: {question} — still running, reply with "
        f"send_msg(to={run_id})"
    )


class SubagentPool:
    """Owns every live run and the tasks driving them."""

    def __init__(
        self,
        deps: RunDeps,
        tools_factory: SubagentToolsFactory,
        deliver: DeliverToSession,
        audit: AuditLog,
        idle_timeout: float,
    ) -> None:
        self._deps = deps
        self._tools_factory = tools_factory
        self._deliver = deliver
        self._audit = audit
        self.idle_timeout = idle_timeout
        self._runs: dict[str, SubagentRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def spawn(
        self, parent_key: str, parent_channel: str, agent: AgentDef, task: str,
        wait: bool = False,
    ) -> str:
        """Start a run. Returns at once, unless ``wait`` — then with the result,
        or early with the run's first question (after which it carries on async)."""
        run = SubagentRun(
            id=new_run_id(parent_key), parent_key=parent_key,
            parent_channel=parent_channel, agent=agent, task=task,
            messages=[
                {"role": "system", "content": f"{PREAMBLE}\n\n{agent.system_prompt}"},
                {"role": "user", "content": task},
            ],
        )
        gated = self._tools_factory(parent_key, parent_channel, run.id)
        tools = RunTools(FilteredTools(gated, agent.tools, subagent=True), run)
        waiter: asyncio.Future[str] | None = None
        if wait:
            waiter = asyncio.get_running_loop().create_future()
            run.waiter = waiter
        self._runs[run.id] = run
        job = asyncio.get_running_loop().create_task(
            drive(run, tools, self._deps, self._result, lambda: self.idle_timeout)
        )
        self._tasks[run.id] = job
        job.add_done_callback(lambda _t: self._forget(run.id))
        if waiter is None:
            return (
                f"spawned {run.id} ({agent.name}) — its result will arrive "
                "as a message"
            )
        return await waiter

    def get(self, run_id: str) -> SubagentRun | None:
        return self._runs.get(run_id)

    def live(self, parent: str | None = None) -> list[SubagentRun]:
        """Live runs, oldest first; only ``parent``'s children when given."""
        return [r for r in self._runs.values() if parent in (None, r.parent_key)]

    def describe(self, parent: str | None = None) -> str:
        """The ``/tasks`` / ``action=list`` listing of :meth:`live`."""
        return describe_all(self.live(parent))

    def deliver(self, run_id: str, text: str) -> None:
        """A message for a run: drained at its next tool step, or wakes it idle."""
        run = self._runs.get(run_id)
        if run is not None:
            run.inbox.append(text)
            run.wake.set()

    def report_to_parent(
        self, run_id: str, message: PeerMessage, warning: str | None
    ) -> None:
        """A run's own ``send_msg`` (a question) on its way to the parent."""
        run = self._runs.get(run_id)
        if run is None:
            return
        run.messaged_parent = True
        if self._interrupt(run, interrupt_text(run.id, message.text)):
            return
        rendered = render(message, subagent_label(run.id, run.agent.name), warning)
        self._deliver(run.parent_key, run.parent_channel, rendered)

    async def cancel(self, run_id: str, by: str) -> bool:
        """Cancel a run and wait for it to wind down; the parent is told
        ``cancelled by <by>``. False when no such run is live."""
        job = self._tasks.get(run_id)
        run = self._runs.get(run_id)
        if job is None or run is None:
            return False
        run.cancelled_by = by
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        return True

    def cancel_children(self, parent_key: str) -> None:
        """Silently cancel a wiped parent's runs (orphans have no one to tell)."""
        for run in self.live(parent_key):
            self._audit.record("subagent_cancelled", id=run.id, parent=parent_key,
                               reason="parent wiped")
            self._tasks[run.id].cancel()

    def snapshot_for_restart(self) -> list[dict[str, Any]]:
        """One notice per live run, for each parent to hear after the reboot."""
        return [
            {
                "parent_key": run.parent_key,
                "parent_channel": run.parent_channel,
                "text": render(
                    PeerMessage(new_id(), run.id, run.parent_key,
                                run.ended("killed by restart")),
                    subagent_label(run.id, run.agent.name),
                ),
            }
            for run in self._runs.values()
        ]

    async def aclose(self) -> None:
        """Silently cancel every run (daemon shutdown)."""
        jobs = list(self._tasks.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    def _result(self, run: SubagentRun, text: str) -> None:
        """A run's ending (result, kill, timeout, failure) to its parent."""
        if self._interrupt(run, text):
            return
        message = PeerMessage(new_id(), run.id, run.parent_key, text)
        rendered = render(message, subagent_label(run.id, run.agent.name))
        self._deliver(run.parent_key, run.parent_channel, rendered)

    @staticmethod
    def _interrupt(run: SubagentRun, text: str) -> bool:
        """Hand ``text`` to a parent blocked in ``wait=true``, if there is one;
        that unblocks it for good — later messages arrive as messages."""
        waiter, run.waiter = run.waiter, None
        if waiter is None or waiter.done():
            return False
        waiter.set_result(text)
        return True

    def _forget(self, run_id: str) -> None:
        self._runs.pop(run_id, None)
        self._tasks.pop(run_id, None)
