"""Approval state machine, routing, and the four-button outcome.

When :func:`~chief.gate.gate.classify` rules ASK, the triggering call blocks here on
an :class:`asyncio.Future` while the owner is shown a card with four buttons:

    ✅ Approve once · ❌ Deny once · ⭐ Always allow · 🚫 Always deny

A button tap calls :meth:`ApprovalManager.resolve`, which persists the terminal state,
writes the audit line, edits the card to show the outcome, and wakes the parked turn.
``Always-*`` actions additionally write a safe-matched policy rule (via
:class:`~chief.gate.policy.PolicyStore`) so the *next* identical call auto-decides. No
decision within ``timeout_seconds`` is a deny (fail-closed).

Routing: owner-task approvals post in the task's own thread (``route == thread_key``);
the guest-originated → Front-Desk branch is the M6 seam (the caller passes a Front-Desk
``route``). The ``ApprovalIO`` contract lives here, beside the manager, so neither the
adapter nor the gate creates an import cycle with :mod:`chief.core.tasks`.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..persistence.approvals import (
    APPROVED,
    DENIED,
    TIMED_OUT,
    create_approval,
    list_pending,
    try_decide,
    try_mark_notified,
)
from .policy import COMMAND_TOOLS, PolicyStore

logger = logging.getLogger("chief.gate.approvals")

#: Pseudo-tool kind for the M11 Opus-escalation card. It isn't a tool call — it routes
#: through the same request/resolve machinery so the auto-detect ask reuses the card UI,
#: but it must never write a tool-policy rule (see :meth:`_persist_rule`). Defined here,
#: not in tasks.py, so tasks can import it without a cycle (tasks → approvals already).
OPUS_ESCALATION_KIND = "OpusEscalation"


class ApprovalAction(Enum):
    """A button on the approval card. Its value doubles as the callback token."""

    APPROVE_ONCE = "approve_once"
    DENY_ONCE = "deny_once"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_DENY = "always_deny"

    @property
    def allowed(self) -> bool:
        """Whether this action lets the tool call proceed."""
        return self in (ApprovalAction.APPROVE_ONCE, ApprovalAction.ALWAYS_ALLOW)

    @property
    def is_always(self) -> bool:
        """Whether this action also writes a persistent policy rule."""
        return self in (ApprovalAction.ALWAYS_ALLOW, ApprovalAction.ALWAYS_DENY)


@dataclass(frozen=True)
class ApprovalCard:
    """What the adapter renders: a preview line plus the four action buttons (by id)."""

    approval_id: int
    text: str


class ApprovalIO(Protocol):
    """How the manager posts/updates approval cards (implemented by the adapter)."""

    async def send_card(self, route: str, card: ApprovalCard) -> str: ...
    async def edit_card(self, msg_ref: str, text: str) -> None: ...


class _Audit(Protocol):
    def log(self, event: dict[str, Any]) -> None: ...


@dataclass
class _Live:
    """In-memory handle to a parked approval (lost on restart; see :meth:`re_arm`)."""

    future: "asyncio.Future[bool] | None"
    msg_ref: str | None
    route: str
    tool_name: str
    tool_input: dict[str, Any] | None


def _preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """A one-line, human-readable summary of the pending tool call."""
    if tool_name == OPUS_ESCALATION_KIND:
        reason = str(tool_input.get("reason", "")).strip()
        return (
            f"Switch to Opus 4.8 for this task? ({reason})"
            if reason
            else "Switch to Opus 4.8 for this task?"
        )
    if tool_name in COMMAND_TOOLS:
        command = str(tool_input.get("command", "")).strip()
        return f"Run: {command}"
    body = json.dumps(tool_input, default=str)
    if len(body) > 300:
        body = body[:297] + "…"
    return f"{tool_name} {body}"


_VERB = {
    ApprovalAction.APPROVE_ONCE: "✅ Approved (once)",
    ApprovalAction.DENY_ONCE: "❌ Denied (once)",
    ApprovalAction.ALWAYS_ALLOW: "⭐ Always allowed",
    ApprovalAction.ALWAYS_DENY: "🚫 Always denied",
}


def _outcome(action: ApprovalAction, decided_by: str, note: str) -> str:
    """The card's post-decision text: verb, any rule note, and who decided (#136)."""
    return f"{_VERB[action]}{note} — by {decided_by}"


class ApprovalManager:
    """Owns parked approvals and resolves them on a button tap or timeout."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        io: ApprovalIO,
        policy: PolicyStore,
        audit: _Audit,
        timeout_seconds: float = 600.0,
        registry: "ApprovalRegistry | None" = None,
    ) -> None:
        self._sf = session_factory
        self._io = io
        self._policy = policy
        self._audit = audit
        self._timeout = timeout_seconds
        self._live: dict[int, _Live] = {}
        #: ``None`` ⇒ no socket routing (current chat-only behavior, #136).
        self._registry = registry

    async def request(
        self,
        *,
        task_id: int | None,
        thread_key: str,
        tier: str,
        tool_name: str,
        tool_input: dict[str, Any],
        route: str,
    ) -> bool:
        """Park the call: persist, post the card, await a decision (or timeout→deny)."""
        preview = _preview(tool_name, tool_input)
        async with self._sf() as session:
            approval = await create_approval(
                session, task_id=task_id, kind=tool_name, payload_preview=preview
            )
            approval_id = approval.id
        # Arm the future *before* posting the card. The card is the only place a
        # resolver learns the approval id, so a tap can't arrive until after this; a
        # synchronous register (no await between the commit above and here) guarantees
        # any later resolve finds a live future to wake, instead of stranding the turn
        # until the fail-closed timeout.
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        live = _Live(
            future=future,
            msg_ref=None,
            route=route,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self._live[approval_id] = live
        if self._registry is not None:
            self._registry.register(approval_id, self)
        try:
            self._audit.log(
                {
                    "event": "approval_requested",
                    "approval_id": approval_id,
                    "thread_key": thread_key,
                    "tier": tier,
                    "tool": tool_name,
                    "route": route,
                }
            )
            live.msg_ref = await self._io.send_card(
                route, ApprovalCard(approval_id=approval_id, text=preview)
            )
            # Guarded requested→notified: a tap may have already decided the row
            # while the card was posting; never overwrite a terminal state.
            async with self._sf() as session:
                await try_mark_notified(session, approval_id)
            return await asyncio.wait_for(future, self._timeout)
        except TimeoutError:
            return await self._expire(approval_id)
        finally:
            self._live.pop(approval_id, None)
            if self._registry is not None:
                self._registry.discard(approval_id)

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> bool:
        """Apply a button tap: atomically decide, (maybe) write a rule, audit, edit.

        The decision is a single ``UPDATE … WHERE state IN (pending)``; only the writer
        that flips the row proceeds, so concurrent taps (or a tap racing the timeout)
        can't double-decide, double-audit, or persist a rule against a row that another
        tap denied. A losing/duplicate tap returns ``False`` (idempotent, silent) — the
        socket surfaces that as ``already_resolved`` (#136); chat adapters ignore it.
        """
        async with self._sf() as session:
            won = await try_decide(
                session,
                approval_id,
                APPROVED if action.allowed else DENIED,
                decided_by=decided_by,
            )
        if not won:
            return False  # unknown or already decided — the atomic UPDATE settled it

        live = self._live.get(approval_id)
        # Decision is durably committed: wake the parked turn and audit before the
        # fallible side-effects, so a rule-persist or card-edit error can't strand the
        # turn until timeout (it surfaces, but the turn already proceeded).
        if live is not None and live.future is not None and not live.future.done():
            live.future.set_result(action.allowed)
        self._audit.log(
            {
                "event": "approval_decided",
                "approval_id": approval_id,
                "action": action.value,
                "allowed": action.allowed,
                "decided_by": decided_by,
            }
        )
        note = await self._persist_rule(action, live) if action.is_always else ""
        if live is not None and live.msg_ref is not None:
            await self._io.edit_card(
                live.msg_ref, _outcome(action, decided_by, note)
            )
        return True

    async def re_arm(self) -> list[int]:
        """Re-register pending approvals on boot so a late tap still records a decision.

        The parked turn itself does not survive a restart (its continuation rides M2's
        notify-and-ask resume); this only keeps the DB row resolvable. Without the live
        tool context an ``Always-*`` tap cannot re-derive a rule, so it falls back to a
        once-only decision.
        """
        async with self._sf() as session:
            rows = await list_pending(session)
        for row in rows:
            self._live.setdefault(
                row.id,
                _Live(
                    future=None,
                    msg_ref=None,
                    route="",
                    tool_name=row.kind,
                    tool_input=None,
                ),
            )
            if self._registry is not None:
                self._registry.register(row.id, self)
        return [row.id for row in rows]

    async def _persist_rule(
        self, action: ApprovalAction, live: _Live | None
    ) -> str:
        if live is None or live.tool_input is None:
            return " (rule not saved — context lost)"
        if live.tool_name == OPUS_ESCALATION_KIND:
            return ""  # a model switch isn't a tool — never write a tool-policy rule
        if action is ApprovalAction.ALWAYS_ALLOW:
            ok = await self._policy.add_allow(live.tool_name, live.tool_input)
        else:
            ok = await self._policy.add_deny(live.tool_name, live.tool_input)
        return "" if ok else " (rule was unsafe — applied once only)"

    async def _expire(self, approval_id: int) -> bool:
        async with self._sf() as session:
            won = await try_decide(
                session, approval_id, TIMED_OUT, decided_by="timeout"
            )
        if won:  # a tap that landed first already decided it — don't double-edit
            self._audit.log(
                {"event": "approval_timed_out", "approval_id": approval_id}
            )
            live = self._live.get(approval_id)
            if live is not None and live.msg_ref is not None:
                await self._io.edit_card(live.msg_ref, "⌛ Timed out — denied.")
        return False


class ApprovalRegistry:
    """Which manager parked a given approval — the cross-stack answer router (#136).

    One registry is shared by every stack, so an ``answer`` frame arriving on the
    client-plane socket resolves through the *owning* stack's manager (the one holding
    the parked future), not whichever manager happens to see the frame. Resolve-once
    arbitration itself stays in :meth:`ApprovalManager.resolve` (the atomic decide).
    """

    def __init__(self) -> None:
        self._owners: dict[int, ApprovalManager] = {}

    def register(self, approval_id: int, manager: "ApprovalManager") -> None:
        self._owners[approval_id] = manager

    def discard(self, approval_id: int) -> None:
        self._owners.pop(approval_id, None)

    async def resolve(
        self, approval_id: int, action: ApprovalAction, *, decided_by: str
    ) -> bool:
        """Route a decision to the owning manager; ``False`` if unknown or decided."""
        manager = self._owners.get(approval_id)
        if manager is None:
            return False
        return await manager.resolve(approval_id, action, decided_by=decided_by)
