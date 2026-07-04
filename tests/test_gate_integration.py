"""End-to-end: synthetic tool calls through the bound PreToolUse hook + can_use_tool.

Drives the two SDK wiring points the gate produces (no real `claude` subprocess) with
ALLOW / NEVER / ASK-approved / ASK-denied / ASK-timeout calls, asserting the permission
result, the waiting/running status flips around an approval, and the audit trail.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.gate.approvals import ApprovalAction, ApprovalManager
from chief.gate.blacklist import Blacklist
from chief.gate.gate import build_can_use_tool, build_pretool_hook
from chief.gate.policy import PolicyStore
from test_approvals import FakeIO, RecordingAudit, _settle


class _Gate:
    """A session's bound hook + can_use_tool plus the state they touch (harness)."""

    def __init__(
        self,
        *,
        policy: PolicyStore,
        approvals: ApprovalManager,
        audit: RecordingAudit,
        io: FakeIO,
        extra_read_only: frozenset[str] = frozenset(),
    ) -> None:
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.io = io
        self.status: list[str] = []

        async def on_waiting() -> None:
            self.status.append("waiting")

        async def on_running() -> None:
            self.status.append("running")

        self.hook = build_pretool_hook(
            thread_key="-100:5",
            tier="owner",
            policy=policy,
            audit=audit,
            blacklist=Blacklist.from_config(),
            extra_read_only=extra_read_only,
        )
        self.can_use_tool = build_can_use_tool(
            task_id=1,
            thread_key="-100:5",
            tier="owner",
            route="-100:5",
            policy=policy,
            approvals=approvals,
            audit=audit,
            on_waiting=on_waiting,
            on_running=on_running,
            blacklist=Blacklist.from_config(),
            extra_read_only=extra_read_only,
        )

    async def run_hook(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        # The SDK types the hook with strict input/output unions; the test drives it
        # with the runtime shapes, so cast to the loose signature for invocation.
        hook = cast(
            Callable[[dict[str, Any], str | None, Any], Awaitable[dict[str, Any]]],
            self.hook,
        )
        out = await hook(
            {"tool_name": tool_name, "tool_input": tool_input}, "tu-1", None
        )
        decision: str = out["hookSpecificOutput"]["permissionDecision"]
        return decision


async def _gate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    never: list[tuple[str, str | None]] | None = None,
    approved: list[tuple[str, str | None]] | None = None,
    timeout: float = 600.0,
    extra_read_only: frozenset[str] = frozenset(),
) -> _Gate:
    io, audit = FakeIO(), RecordingAudit()
    policy = PolicyStore(session_factory, audit=audit)
    await policy.seed(never=never or [], approved=approved or [])
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=timeout,
    )
    return _Gate(
        policy=policy,
        approvals=approvals,
        audit=audit,
        io=io,
        extra_read_only=extra_read_only,
    )


def _events(audit: RecordingAudit, name: str) -> list[dict[str, object]]:
    return [e for e in audit.events if e.get("event") == name]


# ---- ALLOW -------------------------------------------------------------------


async def test_read_only_allows_through_both_callbacks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A non-file read-only tool (no root needed) allows through both callbacks.
    gate = await _gate(session_factory)

    decision = await gate.run_hook("WebSearch", {"query": "x"})
    result = await gate.can_use_tool(
        "WebSearch", {"query": "x"}, ToolPermissionContext()
    )

    assert decision == "allow"
    assert isinstance(result, PermissionResultAllow)
    assert gate.status == []  # no approval round-trip
    assert _events(gate.audit, "tool_call")[0]["decision"] == "allow"


async def test_guest_admin_tool_allows_with_no_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The owner's manage_guest tool mutates state but is owner-initiated + reversible —
    # the engine puts it in extra_read_only so it ALLOWs with no approval card.
    admin = "mcp__chief_guest_admin__manage_guest"
    gate = await _gate(session_factory, extra_read_only=frozenset({admin}))

    decision = await gate.run_hook(admin, {"name": "alice", "action": "block"})
    result = await gate.can_use_tool(
        admin, {"name": "alice", "action": "block"}, ToolPermissionContext()
    )

    assert decision == "allow"
    assert isinstance(result, PermissionResultAllow)
    assert gate.io.cards == []  # never prompted the owner for their own command
    assert gate.status == []


# ---- NEVER -------------------------------------------------------------------


async def test_never_denies_with_no_approval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = await _gate(
        session_factory, never=[("mcp__chief_shell__bash", "rm -rf /tmp/x")]
    )
    call = {"command": "rm -rf /tmp/x"}

    decision = await gate.run_hook("mcp__chief_shell__bash", call)
    result = await gate.can_use_tool(
        "mcp__chief_shell__bash", call, ToolPermissionContext()
    )

    assert decision == "deny"
    assert isinstance(result, PermissionResultDeny)
    assert gate.io.cards == []  # never parked an approval
    assert _events(gate.audit, "tool_denied")


# ---- ASK approved ------------------------------------------------------------


async def test_ask_approved_flips_waiting_then_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = await _gate(session_factory)
    call = {"command": "git push --force origin main"}

    assert await gate.run_hook("mcp__chief_shell__bash", call) == "ask"
    parked = asyncio.ensure_future(
        gate.can_use_tool("mcp__chief_shell__bash", call, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[0][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42"
    )
    result = await parked

    assert isinstance(result, PermissionResultAllow)
    assert gate.status == ["waiting", "running"]  # flipped around the wait


async def test_shell_tool_ask_approved_allows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A blacklisted shell command routes through ASK → approval.
    gate = await _gate(session_factory)
    call = {"command": "npm install -g httpx"}

    assert await gate.run_hook("mcp__chief_shell__bash", call) == "ask"
    parked = asyncio.ensure_future(
        gate.can_use_tool("mcp__chief_shell__bash", call, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[0][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42"
    )
    result = await parked

    assert isinstance(result, PermissionResultAllow)
    assert gate.status == ["waiting", "running"]


# ---- ASK denied --------------------------------------------------------------


async def test_ask_denied_returns_deny(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = await _gate(session_factory)
    call = {"command": "git push --force origin main"}

    parked = asyncio.ensure_future(
        gate.can_use_tool("mcp__chief_shell__bash", call, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[0][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.DENY_ONCE, decided_by="42"
    )
    result = await parked

    assert isinstance(result, PermissionResultDeny)
    assert gate.status == ["waiting", "running"]


# ---- ASK timeout -------------------------------------------------------------


async def test_ask_timeout_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = await _gate(session_factory, timeout=0.01)

    result = await gate.can_use_tool(
        "mcp__chief_shell__bash",
        {"command": "git push --force origin main"},
        ToolPermissionContext(),
    )

    assert isinstance(result, PermissionResultDeny)
    assert _events(gate.audit, "approval_timed_out")
    assert gate.status == ["waiting", "running"]  # restored even on timeout
