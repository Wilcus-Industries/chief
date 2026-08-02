"""The tool gate: thin, code-enforced safety.

NEVER and APPROVED lists decide most calls; read-only tools auto-approve; the
remaining gray zone raises an approval card on the session's own surface. From
a card the owner can approve once or "always allow" a tool, which persists it
to the approved set (#187). A ``gate.ask_when`` argument pulls an otherwise
approved tool back into the card path — see :mod:`chief.gate_policy` for the
rules and for what "always" persists there. Every call that does *not* raise a
card is instead announced on the session's surface as it starts, so an approved
or "always" tool stays visible to the owner instead of running silently.
Everything behavioral lives in prompts — this file only enforces.
"""

import json
import logging
from collections.abc import Awaitable, Callable

from chief.approvals import Approval, ApprovalBroker
from chief.audit import AuditLog
from chief.dispatch import WEB_CHANNEL, Dispatcher
from chief.gate_policy import Decision, GatePolicy, grant_key
from chief.provider.base import ToolCall, ToolSpec
from chief.tools import ToolContext, ToolDispatcher

logger = logging.getLogger(__name__)

# Arguments are rendered into the announcement line; a shell heredoc or a
# whole file body would otherwise flood the owner's channel.
ANNOUNCE_ARG_LIMIT = 160


AskApproval = Callable[[ToolContext, str], Awaitable[Approval]]
AllowAlways = Callable[[str], None]
Announce = Callable[[ToolContext, str], Awaitable[None]]


def approval_asker(dispatcher: Dispatcher, approvals: ApprovalBroker) -> AskApproval:
    """The card path: ask on the calling thread's own surface, and mirror the
    same card to any dashboard client tapped into that thread (#267).

    Shared by the gate's own gray-zone cards and by tools that raise their own
    card (``schedule``'s command creation), so both reach the owner the same way.
    """

    async def ask(ctx: ToolContext, question: str) -> Approval:
        tk = ctx.thread_key
        send = dispatcher.adapter(ctx.channel).send

        async def send_and_card(q: str) -> None:
            dispatcher.tapped(tk, {"type": "approval", "thread": tk, "question": q})
            if ctx.channel != WEB_CHANNEL:  # web's card above is enough (#267)
                await send(tk, q)

        answer = await approvals.ask(tk, question, send_and_card)
        dispatcher.tapped(
            tk, {"type": "approval_resolved", "thread": tk, "verdict": answer.value}
        )
        return answer

    return ask


def announce_text(call: ToolCall, *, denied: bool = False) -> str:
    """The one-line "running this now" notice for a non-card tool call."""
    arguments = json.dumps(call.arguments, default=str)
    if len(arguments) > ANNOUNCE_ARG_LIMIT:
        arguments = arguments[:ANNOUNCE_ARG_LIMIT] + "…"
    suffix = " — denied by the gate" if denied else ""
    return f"⚙ {call.name} {arguments}{suffix}"


class GatedTools:
    """Per-session tool dispatcher: gate + audit around the shared registry."""

    def __init__(
        self,
        *,
        registry: ToolDispatcher,
        policy: GatePolicy,
        audit: AuditLog,
        context: ToolContext,
        ask: AskApproval,
        on_always: AllowAlways | None = None,
        announce: Announce | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._context = context
        self._ask = ask
        self._on_always = on_always or (lambda _name: None)
        self._announce = announce

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        if call.name not in {spec.name for spec in self._registry.specs()}:
            # A hallucinated tool name must never interrupt the owner with an
            # approval card (or worse, persist an "always" for a name that
            # doesn't exist) — fail straight back to the model so it can
            # self-correct. Nothing runs, so nothing is announced either.
            self._record(call, "unknown_tool")
            return await self._registry.dispatch(call, self._context)
        decision = self._policy.decide(call.name, call.arguments)
        # A watched argument outranks every auto-approve, read_only included:
        # the tool may only read, but the argument is what makes it act.
        pending = self._policy.pending_arguments(call.name, call.arguments)
        if decision is Decision.ASK and not pending and call.name in self._read_only():
            decision = Decision.APPROVED
            self._record(call, "read_only")
            await self._announce_call(call, denied=False)
        elif decision is Decision.ASK:
            # The card already shows the call; a second line would double it.
            decision = await self._ask_card(call, pending)
        else:
            self._record(call, f"list:{decision.value}")
            await self._announce_call(call, denied=decision is Decision.NEVER)
        if decision is Decision.NEVER:
            return f"error: tool '{call.name}' denied by the gate"
        return await self._registry.dispatch(call, self._context)

    async def _announce_call(self, call: ToolCall, *, denied: bool) -> None:
        """Tell the owner's channel a call is starting (best effort).

        Sent *before* the tool runs — the point is visibility into work in
        flight, not a result summary. A dead channel must never break the
        call, so a failure here is logged and swallowed.
        """
        if self._announce is None:
            return
        try:
            await self._announce(self._context, announce_text(call, denied=denied))
        except Exception:
            logger.exception("failed to announce tool call %s", call.name)

    async def _ask_card(
        self, call: ToolCall, pending: tuple[str, ...] = ()
    ) -> Decision:
        carries = f" — carries {', '.join(pending)}" if pending else ""
        question = (
            f"approve tool call {call.name}({call.arguments})"
            f"{carries}? yes / always / no"
        )
        answer = await self._ask(self._context, question)
        if answer is Approval.ALWAYS:
            # An ask_when card grants the argument that raised it, not the
            # whole tool — otherwise one tap would approve every other use.
            for grant in [grant_key(call.name, arg) for arg in pending] or [call.name]:
                self._on_always(grant)
        self._record(call, f"card:{answer.value}")
        return Decision.NEVER if answer is Approval.DENY else Decision.APPROVED

    def _read_only(self) -> set[str]:
        return {spec.name for spec in self._registry.specs() if spec.read_only}

    def _record(self, call: ToolCall, outcome: str) -> None:
        self._audit.record(
            "tool_call",
            tool=call.name,
            arguments=call.arguments,
            outcome=outcome,
            thread=self._context.thread_key,
            channel=self._context.channel,
        )
