"""The tool gate: thin, code-enforced safety.

NEVER and APPROVED lists decide most calls; read-only tools auto-approve; the
remaining gray zone raises an approval card on the session's own surface. From
a card the owner can approve once or "always allow" a tool, which persists it
to the approved set (#187). Every call that does *not* raise a card is instead
announced on the session's surface as it starts, so an approved or "always"
tool stays visible to the owner instead of running silently. Everything
behavioral lives in prompts — this file only enforces.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from chief.approvals import Approval, ApprovalBroker
from chief.audit import AuditLog
from chief.dispatch import Dispatcher
from chief.provider.base import ToolCall, ToolSpec
from chief.tools import ToolContext, ToolDispatcher

logger = logging.getLogger(__name__)

# Arguments are rendered into the announcement line; a shell heredoc or a
# whole file body would otherwise flood the owner's channel.
ANNOUNCE_ARG_LIMIT = 160


class Decision(Enum):
    NEVER = "never"
    APPROVED = "approved"
    ASK = "ask"


@dataclass
class GatePolicy:
    """The code-enforced lists; anything on neither list asks.

    ``approved`` is a live set — an "always allow" answer adds to it so the
    tool stops asking for the rest of the process (and is persisted so it
    survives a restart). A ``"*"`` entry in ``approved`` matches every tool
    name (the config-driven "all tools" switch); ``never`` still takes
    precedence over it.
    """

    never: frozenset[str] = frozenset()
    approved: set[str] = field(default_factory=set)

    def decide(self, tool_name: str) -> Decision:
        if tool_name in self.never:
            return Decision.NEVER
        if "*" in self.approved or tool_name in self.approved:
            return Decision.APPROVED
        return Decision.ASK

    def allow_always(self, tool_name: str) -> None:
        self.approved.add(tool_name)


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


def load_approved(path: Path) -> set[str]:
    """Read the persisted "always allow" tool names (empty if absent)."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_approved(names: set[str], path: Path) -> None:
    """Persist the "always allow" tool names, sorted for a stable file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(names)))


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
        decision = self._policy.decide(call.name)
        if decision is Decision.ASK and call.name in self._read_only():
            decision = Decision.APPROVED
            self._record(call, "read_only")
            await self._announce_call(call, denied=False)
        elif decision is Decision.ASK:
            # The card already shows the call; a second line would double it.
            decision = await self._ask_card(call)
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

    async def _ask_card(self, call: ToolCall) -> Decision:
        question = f"approve tool call {call.name}({call.arguments})? yes / always / no"
        answer = await self._ask(self._context, question)
        if answer is Approval.ALWAYS:
            self._on_always(call.name)
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
