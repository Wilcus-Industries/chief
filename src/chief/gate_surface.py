"""Where the gate's output lands: approval cards and the announce line.

An owner thread gets its card and announcements on its own surface, as ever.
A thread whose surface reaches a non-owner (a stranger's 1:1, a group) gets
neither there — nothing is auto-sent to a non-owner (PRD #308). Its card
renders only in the web cockpit (the hub tap, replayed to a late watcher by
``/events``), and a one-line pointer on the owner's primary surface makes sure
the owner notices it. Only an owner answer resolves a card (the dispatcher's
``resolve_approval`` checks the sender), so the stranger can't approve either.
"""

import logging
from collections.abc import Awaitable, Callable

from chief.approvals import Approval, ApprovalBroker
from chief.dispatch import WEB_CHANNEL, Dispatcher
from chief.gate import Announce, AskApproval
from chief.policy import Audience
from chief.tools import ToolContext

logger = logging.getLogger(__name__)

# Tells the owner a card is waiting in a thread they can't answer on.
Pointer = Callable[[str], Awaitable[None]]


def pointer_text(thread_key: str) -> str:
    """The one-line pointer for a card raised in a non-owner thread."""
    return f"approval pending in thread {thread_key} — answer in the web cockpit"


def card_pointer(dispatcher: Dispatcher, target: tuple[str, str]) -> Pointer:
    """Send :func:`pointer_text` on ``target`` (the owner's primary surface).

    Best effort: a dead primary channel must not turn the card into a failed
    tool call — the card still waits in the cockpit — so a send failure is
    logged, not raised."""
    channel, thread = target

    async def point(thread_key: str) -> None:
        try:
            await dispatcher.adapter(channel).send(thread, pointer_text(thread_key))
        except Exception:
            logger.exception("failed to send the card pointer for %s", thread_key)

    return point


def approval_asker(
    dispatcher: Dispatcher,
    approvals: ApprovalBroker,
    audience: Audience | None = None,
    pointer: Pointer | None = None,
) -> AskApproval:
    """The card path: mirror the card to any dashboard client tapped into the
    calling thread (#267), and send it on the thread's own surface only when
    that surface is the owner's; a non-owner thread gets ``pointer`` instead.

    Shared by the gate's own gray-zone cards and by tools that raise their own
    card (``schedule``'s command creation), so both reach the owner the same way.
    ``audience=None`` treats every thread as the owner's (unit-test wiring).
    """

    async def ask(ctx: ToolContext, question: str) -> Approval:
        tk = ctx.thread_key
        owner_surface = audience is None or audience(ctx.channel, tk)

        async def send_and_card(q: str) -> None:
            dispatcher.tapped(tk, {"type": "approval", "thread": tk, "question": q})
            if not owner_surface:
                if pointer is not None:
                    await pointer(tk)
            elif ctx.channel != WEB_CHANNEL:  # web's card above is enough (#267)
                await dispatcher.adapter(ctx.channel).send(tk, q)

        answer = await approvals.ask(tk, question, send_and_card)
        dispatcher.tapped(
            tk, {"type": "approval_resolved", "thread": tk, "verdict": answer.value}
        )
        return answer

    return ask


def channel_announcer(dispatcher: Dispatcher, audience: Audience) -> Announce:
    """Announce a starting tool call on the thread's own surface — never on a
    non-owner one, where it would leak the agent's work to a stranger."""

    async def announce(ctx: ToolContext, text: str) -> None:
        if audience(ctx.channel, ctx.thread_key):
            await dispatcher.adapter(ctx.channel).send(ctx.thread_key, text)

    return announce
