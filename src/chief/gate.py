"""The tool gate: thin, code-enforced safety.

NEVER and APPROVED lists decide most calls; the gray zone raises an approval
card on the session's own surface. Everything behavioral lives in prompts —
this file only enforces.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from chief.agent.tools import ToolContext, ToolDispatcher
from chief.audit import AuditLog
from chief.provider.base import ToolCall, ToolSpec


class Decision(Enum):
    NEVER = "never"
    APPROVED = "approved"
    ASK = "ask"


@dataclass(frozen=True)
class GatePolicy:
    """The two code-enforced lists; anything on neither list asks."""

    never: frozenset[str] = frozenset()
    approved: frozenset[str] = frozenset()

    def decide(self, tool_name: str) -> Decision:
        if tool_name in self.never:
            return Decision.NEVER
        if tool_name in self.approved:
            return Decision.APPROVED
        return Decision.ASK


AskApproval = Callable[[ToolContext, str], Awaitable[bool]]


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
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._context = context
        self._ask = ask

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        decision = self._policy.decide(call.name)
        if decision is Decision.ASK:
            question = f"approve tool call {call.name}({call.arguments})? yes/no"
            allowed = await self._ask(self._context, question)
            decision = Decision.APPROVED if allowed else Decision.NEVER
            self._record(call, f"card:{decision.value}")
        else:
            self._record(call, f"list:{decision.value}")
        if decision is Decision.NEVER:
            return f"error: tool '{call.name}' denied by the gate"
        return await self._registry.dispatch(call, self._context)

    def _record(self, call: ToolCall, outcome: str) -> None:
        self._audit.record(
            "tool_call",
            tool=call.name,
            arguments=call.arguments,
            outcome=outcome,
            thread=self._context.thread_key,
            channel=self._context.channel,
        )
