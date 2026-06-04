"""The classifier and the two SDK callbacks it drives.

One function, :func:`classify`, is the single source of truth for every tool call,
implementing DESIGN's decision order:

    NEVER → DENY · read-only/safe → ALLOW · APPROVED → ALLOW · else effectful → ASK

Two SDK wiring points consume it (DESIGN: Verified — gate split):

- :func:`build_pretool_hook` — a ``PreToolUse`` hook that runs on *every* call, returns
  a fast ``allow``/``deny``/``ask`` decision, and writes the audit line.
- :func:`build_can_use_tool` — owns the ``ask`` → approval round-trip. It re-runs
  :func:`classify` defensively so a NEVER rule can never slip through even if the SDK
  maps "ask" straight to the callback.

Both factories bind to one session's ``(thread_key, tier, policy, audit)`` plus, for the
``can_use_tool`` side, the shared :class:`~chief.gate.approvals.ApprovalManager`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)
from claude_agent_sdk.types import HookCallback, HookContext

from ..persistence.policy import NEVER
from .policy import PolicyStore


class GateDecision(Enum):
    """How the gate ruled on a tool call. The value is the SDK permission string."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class Verdict:
    """A gate ruling plus a human-readable reason (shown to Claude / logged)."""

    decision: GateDecision
    reason: str


def _always(_: dict[str, Any]) -> bool:
    return True


#: Read-only tool → predicate over its input. Seeded with the built-ins later milestones
#: use; effectful tools (calendar writes, shell, Gmail send) are deliberately absent, so
#: they fall through to ASK. Unknown tool ⇒ ASK (safe default).
READ_ONLY: dict[str, Callable[[dict[str, Any]], bool]] = {
    "Read": _always,
    "Glob": _always,
    "Grep": _always,
    "WebSearch": _always,
    # WebFetch is the GET-only built-in, so any input is read-only. A web tool that can
    # POST must NOT reuse this name with ``_always`` — gate it with a method predicate.
    "WebFetch": _always,
}


def is_read_only(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Whether ``tool_name`` is a known read-only/safe tool for this input."""
    predicate = READ_ONLY.get(tool_name)
    return predicate is not None and predicate(tool_input)


#: File-op tools chief gets at M4 — read-only, but confined to the memory dir (no
#: workspace until M7). A relative/absent path resolves against the session ``cwd``
#: (the memory root), so it is in-bounds; an absolute path that escapes is denied.
FILE_OP_TOOLS = frozenset({"Read", "Glob", "Grep"})


def confined_to_memory(tool_input: dict[str, Any], memory_dir: str) -> bool:
    """Whether a file-op call's path stays within ``memory_dir``.

    An absent/relative path is in-bounds (it resolves against the memory-root ``cwd``);
    an absolute or ``..``-escaping path is checked against the resolved memory root.
    """
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw:
        return True
    root = Path(memory_dir)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def classify(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PolicyStore,
    *,
    memory_dir: str | None = None,
    extra_read_only: frozenset[str] = frozenset(),
) -> Verdict:
    """Rule on a tool call: NEVER→DENY, read-only→ALLOW, APPROVED→ALLOW, else ASK.

    When ``memory_dir`` is set (M4), a file-op tool (Read/Glob/Grep) is ALLOWed only
    while its path stays inside the memory dir and DENied otherwise — chief's reads are
    confined to memory until a workspace lands (M7).

    ``extra_read_only`` names tools an MCP layer has declared read-only (e.g. the
    calendar list/free-busy tools, M5); they ALLOW like the built-ins, keeping
    :mod:`gate` decoupled from any specific MCP. NEVER still wins over them.
    """
    listed = policy.classify_against(tool_name, tool_input)
    if listed == NEVER:
        return Verdict(GateDecision.DENY, f"{tool_name} is on the NEVER list")
    if memory_dir is not None and tool_name in FILE_OP_TOOLS:
        if confined_to_memory(tool_input, memory_dir):
            return Verdict(GateDecision.ALLOW, f"{tool_name} reads within memory")
        return Verdict(GateDecision.DENY, f"{tool_name} path is outside memory")
    if is_read_only(tool_name, tool_input) or tool_name in extra_read_only:
        return Verdict(GateDecision.ALLOW, f"{tool_name} is read-only")
    if listed is not None:  # APPROVED
        return Verdict(GateDecision.ALLOW, f"{tool_name} is pre-approved")
    return Verdict(GateDecision.ASK, f"{tool_name} needs the owner's approval")


class _Approver(Protocol):
    """The slice of :class:`~chief.gate.approvals.ApprovalManager` the gate calls."""

    async def request(
        self,
        *,
        task_id: int | None,
        thread_key: str,
        tier: str,
        tool_name: str,
        tool_input: dict[str, Any],
        route: str,
    ) -> bool: ...


class _Audit(Protocol):
    def log(self, event: dict[str, Any]) -> None: ...


StatusHook = Callable[[], Awaitable[None]]


def build_pretool_hook(
    *,
    thread_key: str,
    tier: str,
    policy: PolicyStore,
    audit: _Audit,
    memory_dir: str | None = None,
    extra_read_only: frozenset[str] = frozenset(),
) -> HookCallback:
    """A ``PreToolUse`` hook: classify, audit, return the permission decision."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        verdict = classify(
            tool_name,
            tool_input,
            policy,
            memory_dir=memory_dir,
            extra_read_only=extra_read_only,
        )
        audit.log(
            {
                "event": "tool_call",
                "thread_key": thread_key,
                "tier": tier,
                "tool": tool_name,
                "decision": verdict.decision.value,
                "reason": verdict.reason,
            }
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict.decision.value,
                "permissionDecisionReason": verdict.reason,
            }
        }

    # The SDK's HookCallback types ``input_data`` as a hook-input union and the result
    # as an output TypedDict; this PreToolUse-only hook reads ``dict``/returns a
    # permission dict, so cast to the SDK contract at the boundary (same runtime shape).
    return cast(HookCallback, hook)


def build_can_use_tool(
    *,
    task_id: int | None,
    thread_key: str,
    tier: str,
    route: str,
    policy: PolicyStore,
    approvals: _Approver,
    audit: _Audit,
    on_waiting: StatusHook | None = None,
    on_running: StatusHook | None = None,
    memory_dir: str | None = None,
    extra_read_only: frozenset[str] = frozenset(),
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]:
    """A ``can_use_tool`` callback owning the ASK → approval round-trip.

    Re-runs :func:`classify` so ALLOW/DENY short-circuit without a prompt and a NEVER
    rule is enforced even if the SDK routed an "ask" straight here. For ASK it flips the
    task to ``waiting``, awaits the owner's decision (or timeout→deny), and restores
    ``running``.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        verdict = classify(
            tool_name,
            tool_input,
            policy,
            memory_dir=memory_dir,
            extra_read_only=extra_read_only,
        )
        if verdict.decision is GateDecision.ALLOW:
            return PermissionResultAllow()
        if verdict.decision is GateDecision.DENY:
            audit.log(
                {
                    "event": "tool_denied",
                    "thread_key": thread_key,
                    "tool": tool_name,
                    "reason": verdict.reason,
                }
            )
            return PermissionResultDeny(message=verdict.reason)

        if on_waiting is not None:
            await on_waiting()
        try:
            allowed = await approvals.request(
                task_id=task_id,
                thread_key=thread_key,
                tier=tier,
                tool_name=tool_name,
                tool_input=tool_input,
                route=route,
            )
        finally:
            if on_running is not None:
                await on_running()
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by the owner.")

    return can_use_tool
