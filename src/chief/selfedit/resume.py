"""Boot-side: answer the turns the previous process died holding.

``chief.selfedit.inflight`` lists them. Per thread, in the order they were
queued: a turn cut off mid-way gets one system wake (its user message and any
finished tool calls are already in history, and a dangling call is marked
interrupted when the session loads), then each held or queued message is
replayed verbatim with its original sender. Threads run concurrently, each in
its own background task, so a long resume never holds up the boot.
"""

import asyncio
import logging
from typing import Protocol

from chief.adapters.base import Message
from chief.selfedit.inflight import Interrupted

logger = logging.getLogger(__name__)

SYSTEM = "system"
RESUME_NOTE = (
    "[chief restarted] The daemon restarted while your previous turn in this "
    "thread was still running, so it was cut off before it finished. Any tool "
    "call left open is marked interrupted. If the task is still wanted, pick "
    "up where you left off; otherwise tell the owner where you stopped."
)


class Replayer(Protocol):
    """What resuming needs of the dispatcher (``Dispatcher.replay``)."""

    async def replay(self, message: Message) -> None: ...


def resume_plan(turns: list[Interrupted]) -> dict[str, list[Message]]:
    """Each thread's messages to run, in order."""
    plan: dict[str, list[Message]] = {}
    for turn in turns:
        queue = plan.setdefault(turn.thread_key, [])
        if turn.started:
            # One wake per thread is enough, however many turns were cut.
            if not any(m.text == RESUME_NOTE for m in queue):
                queue.insert(0, Message(turn.channel, SYSTEM, turn.thread_key,
                                        RESUME_NOTE))
        else:
            queue.append(
                Message(turn.channel, turn.sender, turn.thread_key, turn.text)
            )
    return plan


def resume_interrupted(
    dispatcher: Replayer, turns: list[Interrupted]
) -> list[asyncio.Task[None]]:
    """Start one task per thread; the caller keeps the tasks referenced."""

    async def run(thread: str, messages: list[Message]) -> None:
        logger.info("resuming %d interrupted turn(s) on %s", len(messages), thread)
        for message in messages:
            try:
                await dispatcher.replay(message)
            except Exception:
                logger.exception("could not resume a turn on %s", thread)

    return [
        asyncio.get_running_loop().create_task(run(thread, messages))
        for thread, messages in resume_plan(turns).items()
    ]
