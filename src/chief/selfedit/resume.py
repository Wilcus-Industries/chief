"""Boot-side: answer the messages the previous process died holding.

``chief.selfedit.inflight`` lists them. Per thread, in the order they arrived:
a turn cut off mid-way gets one system wake (its user message and any finished
tool calls are already in history, and a dangling call is marked interrupted
when the session loads), then each queued, injected or held message is
replayed verbatim with its original sender.

Order against new traffic: the boot ``hold``s each resumed thread on the
dispatcher *before* the adapters start, so a message sent while chief was down
("never mind") can't overtake the older one it answers ("do X"); each thread
is released once its resume finishes. Threads resume concurrently, each in its
own background task, so a long resume never holds up the boot.
"""

import asyncio
import dataclasses
import logging
from typing import Protocol

from chief.adapters.base import Message
from chief.agent.restart_gate import TurnTicket
from chief.selfedit.inflight import Interrupted

logger = logging.getLogger(__name__)

SYSTEM = "system"
RESUME_NOTE = (
    "[chief restarted] The daemon restarted while your previous turn in this "
    "thread was still running, so it was cut off before it finished. Any tool "
    "call left open is marked interrupted. If the task is still wanted, pick "
    "up where you left off; otherwise tell the owner where you stopped."
)


@dataclasses.dataclass(frozen=True)
class Resume:
    """One turn to run: the record it answers, plus any it makes redundant
    (a second cut-off turn on a thread that already gets a wake)."""

    message: Message
    turn_id: str
    absorbs: tuple[str, ...] = ()


class Replayer(Protocol):
    """What resuming needs of the dispatcher."""

    def hold(self, threads: list[str]) -> None: ...

    def release(self, thread_key: str) -> None: ...

    async def replay(self, message: Message, ticket: TurnTicket) -> None: ...


class Records(Protocol):
    """What resuming needs of the in-flight record."""

    def adopt(self, turn_id: str) -> TurnTicket: ...

    def forget(self, turn_id: str) -> None: ...


def resume_plan(turns: list[Interrupted]) -> dict[str, list[Resume]]:
    """Each thread's turns to run, in order: its wake first, if it has one."""
    plan: dict[str, list[Resume]] = {}
    for turn in turns:
        queue = plan.setdefault(turn.thread_key, [])
        if not turn.started:
            message = Message(turn.channel, turn.sender, turn.thread_key, turn.text)
            queue.append(Resume(message, turn.turn_id))
        elif queue and queue[0].message.text == RESUME_NOTE:
            queue[0] = dataclasses.replace(
                queue[0], absorbs=(*queue[0].absorbs, turn.turn_id)
            )
        else:
            wake = Message(turn.channel, SYSTEM, turn.thread_key, RESUME_NOTE)
            queue.insert(0, Resume(wake, turn.turn_id))
    return plan


def resume_interrupted(
    dispatcher: Replayer, records: Records, plan: dict[str, list[Resume]]
) -> list[asyncio.Task[None]]:
    """Start one task per thread (already ``hold``-ed by the boot); the caller
    keeps the tasks referenced. Each record is adopted only as its turn runs,
    so the rest of a thread's list stays on disk if this process dies too."""

    async def run(thread: str, items: list[Resume]) -> None:
        logger.info("resuming %d interrupted turn(s) on %s", len(items), thread)
        try:
            for item in items:
                for absorbed in item.absorbs:
                    records.forget(absorbed)
                try:
                    await dispatcher.replay(item.message, records.adopt(item.turn_id))
                except Exception:
                    logger.exception("could not resume a turn on %s", thread)
        finally:
            dispatcher.release(thread)

    return [
        asyncio.get_running_loop().create_task(run(thread, items))
        for thread, items in plan.items()
    ]
