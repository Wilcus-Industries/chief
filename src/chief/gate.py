"""The tool gate: thin, code-enforced safety.

NEVER and APPROVED lists decide most calls; read-only tools auto-approve; the
remaining gray zone raises an approval card on the session's own surface. From
a card the owner can approve once or "always allow" a tool, which persists it
to the approved set (#187). Everything behavioral lives in prompts — this file
only enforces.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from chief.agent.tools import ToolContext, ToolDispatcher
from chief.approvals import Approval
from chief.audit import AuditLog
from chief.provider.base import ToolCall, ToolSpec


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
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._context = context
        self._ask = ask
        self._on_always = on_always or (lambda _name: None)

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()

    async def dispatch(
        self, call: ToolCall, context: ToolContext | None = None
    ) -> str:
        if call.name not in {spec.name for spec in self._registry.specs()}:
            # A hallucinated tool name must never interrupt the owner with an
            # approval card (or worse, persist an "always" for a name that
            # doesn't exist) — fail straight back to the model so it can
            # self-correct.
            self._record(call, "unknown_tool")
            return await self._registry.dispatch(call, self._context)
        decision = self._policy.decide(call.name)
        if decision is Decision.ASK and call.name in self._read_only():
            decision = Decision.APPROVED
            self._record(call, "read_only")
        elif decision is Decision.ASK:
            decision = await self._ask_card(call)
        else:
            self._record(call, f"list:{decision.value}")
        if decision is Decision.NEVER:
            return f"error: tool '{call.name}' denied by the gate"
        return await self._registry.dispatch(call, self._context)

    async def _ask_card(self, call: ToolCall) -> Decision:
        question = (
            f"approve tool call {call.name}({call.arguments})? "
            "yes / always / no"
        )
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
