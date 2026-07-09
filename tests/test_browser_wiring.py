"""Gate wiring and guest isolation for the browser (playwright) MCP service.

Covers two broad areas:

1. **Structural wiring** — session kwargs inspect: read tools in allowed_tools, write
   tools absent, MCP server registered, tier isolation for guests.

2. **Approval-gate round-trip** — browser write tools (click, fill, evaluate, …) route
   through ``can_use_tool`` → approval card; always-allow suppresses subsequent cards
   for that tool; one-time allow does not; the arbitrary-JS tools
   (``browser_evaluate`` / ``browser_run_code_unsafe``) are never pre-approvable via
   ``extra_read_only``.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    ToolPermissionContext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import Attachment
from chief.core.session import Final, TurnEvent
from chief.core.tasks import (
    MEMORY_TOOLS,
    WEB_META_TOOLS,
    SessionProto,
    TaskManager,
)
from chief.gate.approvals import ApprovalAction, ApprovalManager
from chief.gate.blacklist import Blacklist
from chief.gate.gate import build_can_use_tool, build_pretool_hook
from chief.gate.policy import PolicyStore
from chief.memory.store import Fact
from chief.memory.versioning import NullVersioner, Versioner
from chief.tools.browser import mcp as browser_mcp
from test_approvals import FakeIO, RecordingAudit, _settle

Factory = Callable[..., SessionProto]


class _FakeMemory:
    def __init__(self) -> None:
        self._versioner: Versioner = NullVersioner()

    @property
    def versioner(self) -> Versioner:
        return self._versioner

    def facts_listing(self) -> str:
        return ""

    def soul(self) -> str:
        return "# Soul\nI am chief."

    def user(self) -> str:
        return "# Will"

    def list_facts(self, namespace: str) -> list[Fact]:
        return []

    async def forget(self, namespace: str, query: str) -> list[Fact]:
        raise NotImplementedError

    async def purge_expired(self) -> int:
        raise NotImplementedError

    async def ensure_scaffold(self) -> None:
        return None


class _FakeSession:
    def __init__(self, *, model: str, resume: str | None = None, **_: Any) -> None:
        self.model = model
        self.resume = resume
        self.session_id = resume
        self.last_cost_usd = 0.0
        self.last_rate_limit_status: str | None = None
        self.last_served_model: str | None = None

    async def run_turn(
        self, text: str, attachments: Sequence[Attachment] = ()
    ) -> "AsyncIterator[TurnEvent]":
        yield Final(text=f"reply:{text}")

    async def interrupt(self) -> None:
        pass

    async def set_model(self, model: str) -> None:
        self.model = model

    async def aclose(self) -> None:
        pass


def _capture_factory(captured: dict[str, Any]) -> Factory:
    def factory(**kwargs: Any) -> SessionProto:
        captured.clear()
        captured.update(kwargs)
        return _FakeSession(model=kwargs["model"], resume=kwargs.get("resume"))

    return factory


async def _no(*args: Any, **kwargs: Any) -> bool:
    return False


def _browser_manager(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    factory: Factory,
    enabled: bool = True,
) -> TaskManager:
    services = (
        (browser_mcp.service("http://mcp-playwright:3000/mcp"),) if enabled else ()
    )
    return TaskManager(
        session_factory=session_factory,
        io=_FakeIO(),
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=_FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        google_services=services,
    )


class _FakeIO:
    async def send(self, thread_key: str, text: str) -> None:
        pass

    async def send_file(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        return "new"

    async def archive_thread(self, thread_key: str) -> None:
        pass


# ---- catalog unit tests (partition shape) ----------------------------------------


def test_service_shape_mirrors_google_services() -> None:
    svc = browser_mcp.service("http://mcp-playwright:3000/mcp")
    assert svc.name == "browser"
    assert svc.server_name == "playwright"
    assert svc.read_tools is browser_mcp.READ_TOOLS
    assert svc.write_tools is browser_mcp.WRITE_TOOLS
    assert svc.deferred_tools == ()
    assert svc.server_config() == {
        "type": "http",
        "url": "http://mcp-playwright:3000/mcp",
    }


# ---- owner session wiring --------------------------------------------------------


async def test_owner_browser_session_wires_mcp_and_read_tools_in_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Read tools must be in allowed_tools so the gate pre-approves them (no card).
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    assert captured["mcp_servers"] == {
        "playwright": {"type": "http", "url": "http://mcp-playwright:3000/mcp"}
    }
    allowed = captured["allowed_tools"]
    # A sample of read tools must be in allowed_tools.
    assert "mcp__playwright__browser_navigate" in allowed
    assert "mcp__playwright__browser_snapshot" in allowed
    assert "mcp__playwright__browser_take_screenshot" in allowed
    assert "mcp__playwright__browser_console_messages" in allowed
    assert "mcp__playwright__browser_wait_for" in allowed
    assert "mcp__playwright__browser_tabs" in allowed
    # Memory + web tools are retained alongside browser tools.
    assert "Read" in allowed
    assert "WebSearch" in allowed
    await mgr.shutdown()


async def test_owner_browser_write_tools_absent_from_allowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Write tools must be absent from allowed_tools so they reach approval.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    allowed = set(captured["allowed_tools"])
    for write_tool in browser_mcp.WRITE_TOOLS:
        assert write_tool not in allowed, (
            f"{write_tool!r} must NOT be in allowed_tools (it is a write tool)"
        )
    await mgr.shutdown()


async def test_owner_browser_gate_read_set_includes_read_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Read tools are fed to the gate's extra_read_only so the PreToolUse hook
    # allows them with no card (same as calendar reads).
    # We verify this indirectly: the tools appear in allowed_tools AND they are all
    # in browser_mcp.READ_TOOLS (not in WRITE_TOOLS) — correct partition confirms
    # the gate sees them as pre-approved.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    allowed = set(captured["allowed_tools"])
    for read_tool in browser_mcp.READ_TOOLS:
        assert read_tool in allowed, (
            f"{read_tool!r} must be in allowed_tools (pre-approved read tool)"
        )
    await mgr.shutdown()


# ---- guest isolation tests -------------------------------------------------------


async def test_guest_session_has_no_browser_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Browser tools must be completely absent from guest sessions (tier isolation).
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:9", tier="guest")

    allowed = set(captured.get("allowed_tools", []))
    # No browser tool — neither read nor write — must appear in a guest session.
    for tool in list(browser_mcp.READ_TOOLS) + list(browser_mcp.WRITE_TOOLS):
        assert tool not in allowed, (
            f"browser tool {tool!r} leaked into guest session"
        )
    # No playwright MCP server registered for the guest.
    assert "playwright" not in captured.get("mcp_servers", {})
    await mgr.shutdown()


async def test_guest_session_has_empty_allowed_tools_with_browser_manager(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A guest with no calendar/guest service wired gets an empty allowed list —
    # browser tools never bleed through even when the manager has them for the owner.
    captured: dict[str, Any] = {}
    mgr = _browser_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:9", tier="guest")

    # No mcp_servers for the guest (playwright is owner-only).
    assert "mcp_servers" not in captured
    # allowed_tools is empty — no browser or owner tools leaked.
    assert captured["allowed_tools"] == []
    await mgr.shutdown()


# ---- disabled browser -------------------------------------------------------


async def test_browser_disabled_owner_keeps_memory_and_web_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _browser_manager(
        session_factory, factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    # No playwright MCP server when disabled.
    assert "mcp_servers" not in captured
    # Owner still gets memory + web tools.
    assert set(captured["allowed_tools"]) == set(MEMORY_TOOLS) | set(WEB_META_TOOLS)
    await mgr.shutdown()


# ---- approval-gate round-trip tests -----------------------------------------
#
# These tests use the low-level gate helpers (build_pretool_hook / build_can_use_tool)
# directly so they exercise the hook + approval machinery without needing a real SDK
# subprocess.  The pattern mirrors test_gate_integration — the _BrowserGate harness
# binds an owner gate the way tasks.py does. Under the default-allow posture browser
# write tools run freely; only a blacklist entry (configurable blacklist_tools) still
# routes one to the approval card.


class _BrowserGate:
    """Owner gate: default-allow, with an optional blacklist re-gating named tools."""

    def __init__(
        self,
        *,
        policy: PolicyStore,
        approvals: ApprovalManager,
        audit: RecordingAudit,
        io: FakeIO,
        blacklist: Blacklist | None = None,
    ) -> None:
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.io = io
        self.status: list[str] = []

        # Mirror tasks.py _build_gate: browser read tools go into extra_read_only
        # so the gate ALLOWs them with no card; write tools are absent from here.
        extra_read_only = frozenset(browser_mcp.READ_TOOLS)

        async def on_waiting() -> None:
            self.status.append("waiting")

        async def on_running() -> None:
            self.status.append("running")

        self.hook = build_pretool_hook(
            thread_key="-100:5",
            tier="owner",
            policy=policy,
            audit=audit,
            blacklist=blacklist,
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
            blacklist=blacklist,
            extra_read_only=extra_read_only,
        )

    async def run_hook(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        hook = cast(
            Callable[[dict[str, Any], str | None, Any], Any],
            self.hook,
        )
        out = await hook(
            {"tool_name": tool_name, "tool_input": tool_input}, "tu-1", None
        )
        decision: str = out["hookSpecificOutput"]["permissionDecision"]
        return decision


async def _browser_gate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    approved: list[tuple[str, str | None]] | None = None,
    timeout: float = 600.0,
    blacklist_tools: tuple[str, ...] = (),
) -> _BrowserGate:
    io, audit = FakeIO(), RecordingAudit()
    policy = PolicyStore(session_factory, audit=audit)
    await policy.seed(approved=approved or [])
    approvals = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=timeout,
    )
    return _BrowserGate(
        policy=policy,
        approvals=approvals,
        audit=audit,
        io=io,
        blacklist=Blacklist.from_config(tools=blacklist_tools),
    )


# ---- write tools allowed by default ------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_type",
        "mcp__playwright__browser_fill_form",
        "mcp__playwright__browser_select_option",
        "mcp__playwright__browser_drag",
        "mcp__playwright__browser_drop",
        "mcp__playwright__browser_file_upload",
        "mcp__playwright__browser_handle_dialog",
        "mcp__playwright__browser_press_key",
        "mcp__playwright__browser_hover",
        "mcp__playwright__browser_evaluate",
        "mcp__playwright__browser_run_code_unsafe",
        "mcp__playwright__browser_close",
        "mcp__playwright__browser_resize",
    ],
)
async def test_write_tool_allows_by_default(
    tool_name: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Default-allow posture: an owner browser write runs with no card unless the
    # owner has blacklisted it.
    gate = await _browser_gate(session_factory)
    tool_input: dict[str, Any] = {}

    hook_decision = await gate.run_hook(tool_name, tool_input)
    result = await gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())

    assert hook_decision == "allow"
    assert isinstance(result, PermissionResultAllow)
    assert gate.io.cards == []


# ---- a blacklisted browser tool still routes to the card ----------------------


async def test_blacklisted_browser_tool_routes_to_approval_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # blacklist_tools re-gates a named tool: hook ASKs and can_use_tool parks at the
    # approval card, exactly like the old default-ask flow.
    tool_name = "mcp__playwright__browser_run_code_unsafe"
    gate = await _browser_gate(session_factory, blacklist_tools=(tool_name,))
    tool_input: dict[str, Any] = {}

    hook_decision = await gate.run_hook(tool_name, tool_input)
    assert hook_decision == "ask"

    parked = asyncio.ensure_future(
        gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[-1][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.APPROVE_ONCE, decided_by="owner"
    )
    result = await parked

    assert isinstance(result, PermissionResultAllow)
    assert gate.status == ["waiting", "running"]


# ---- read tools pre-approved, no card ---------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    list(browser_mcp.READ_TOOLS),
)
async def test_read_tool_allows_with_no_card(
    tool_name: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Read tools are in extra_read_only — hook + can_use_tool both ALLOW with no card.
    gate = await _browser_gate(session_factory)
    tool_input: dict[str, Any] = {}

    hook_decision = await gate.run_hook(tool_name, tool_input)
    result = await gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())

    assert hook_decision == "allow"
    assert isinstance(result, PermissionResultAllow)
    assert gate.io.cards == []


# ---- always-allow suppresses subsequent cards --------------------------------


async def test_always_allow_suppresses_subsequent_card_for_that_tool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # After an always-allow tap on a blacklisted tool, the same call auto-ALLOWs next
    # time (the APPROVED rule beats the blacklist).
    tool_name = "mcp__playwright__browser_click"
    gate = await _browser_gate(session_factory, blacklist_tools=(tool_name,))
    tool_input: dict[str, Any] = {}

    # First call: routes to card, resolved with always-allow.
    parked = asyncio.ensure_future(
        gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[0][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.ALWAYS_ALLOW, decided_by="owner"
    )
    first_result = await parked
    assert isinstance(first_result, PermissionResultAllow)

    # Second call for the same tool + input: must ALLOW with no new card.
    card_count_before = len(gate.io.cards)
    second_result = await gate.can_use_tool(
        tool_name, tool_input, ToolPermissionContext()
    )
    assert isinstance(second_result, PermissionResultAllow)
    assert len(gate.io.cards) == card_count_before, (
        "always-allow should suppress the approval card on repeat calls"
    )


# ---- one-time allow does NOT suppress subsequent cards -----------------------


async def test_one_time_allow_does_not_suppress_subsequent_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # After an approve-once tap, the same blacklisted tool still routes to a new card.
    tool_name = "mcp__playwright__browser_fill_form"
    gate = await _browser_gate(session_factory, blacklist_tools=(tool_name,))
    tool_input: dict[str, Any] = {}

    # First call: routes to card, resolved with approve-once.
    parked = asyncio.ensure_future(
        gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())
    )
    await _settle(lambda: bool(gate.io.cards))
    approval_id = gate.io.cards[0][1].approval_id
    await gate.approvals.resolve(
        approval_id, ApprovalAction.APPROVE_ONCE, decided_by="owner"
    )
    first_result = await parked
    assert isinstance(first_result, PermissionResultAllow)

    # Second call: must still park at a new approval card (no policy rule was written).
    parked2 = asyncio.ensure_future(
        gate.can_use_tool(tool_name, tool_input, ToolPermissionContext())
    )
    await _settle(lambda: len(gate.io.cards) >= 2)
    approval_id2 = gate.io.cards[1][1].approval_id
    await gate.approvals.resolve(
        approval_id2, ApprovalAction.APPROVE_ONCE, decided_by="owner"
    )
    second_result = await parked2

    assert isinstance(second_result, PermissionResultAllow)
    assert len(gate.io.cards) == 2, (
        "one-time allow must NOT suppress the card on the next identical call"
    )


# ---- evaluate / run_code_unsafe stay out of the read partition ----------------


async def test_arbitrary_js_tools_stay_out_of_read_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The arbitrary-JS tools stay in the write partition (never READ_TOOLS), so an
    # owner who blacklists them (blacklist_tools) re-gates them cleanly and a guest
    # never sees them at all.
    read_only_names = frozenset(browser_mcp.READ_TOOLS)
    assert "mcp__playwright__browser_evaluate" not in read_only_names
    assert "mcp__playwright__browser_run_code_unsafe" not in read_only_names
