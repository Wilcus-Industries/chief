"""Opt-in live test: does the Copilot runtime gate an in-process ``@define_tool`` call?

This is the spike-landmine-1 confirmation the mocked tests cannot make. The mocked suite
(``test_copilot_gate.py``) proves the *adapter* gates a ``custom-tool`` permission
request once it arrives, and that chief's real gate rules on it — but *whether the
runtime actually raises that request (or fires the pre-tool hook) for a custom tool
registered in-process* is a runtime behaviour only the real Copilot binary can answer.

Skipped unless ``CHIEF_COPILOT_LIVE`` is set (needs a Copilot login + the downloaded
runtime; see ``test_copilot_backend_live.py`` for setup). It registers a custom
``@define_tool`` (seeded APPROVED so the gate allows it with no card and the turn cannot
hang on an approval), wraps chief's real permission handler + pre-tool hook in spies,
and drives a turn that should call the tool — then asserts the custom-tool call was seen
at a gate-governed surface. If it fires at neither, the runtime bypasses the gate for
custom tools and the adapter would need a different route (the landmine); the assertion
message says exactly that.
"""

import asyncio
import os
from typing import cast

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookCallback
from copilot import (
    CopilotClient,
    CopilotSession,
    PermissionRequest,
    PermissionRequestResult,
    PreToolUseHookInput,
    PreToolUseHookOutput,
    SessionHooks,
    define_tool,
)
from copilot.session_events import SessionEvent, SessionIdleData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief.core.copilot_gate import build_permission_handler, build_session_hooks
from chief.gate.approvals import ApprovalManager
from chief.gate.gate import build_can_use_tool, build_pretool_hook
from chief.gate.policy import PolicyStore
from chief.persistence.models import Base
from test_approvals import FakeIO, RecordingAudit

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_COPILOT_LIVE"),
    reason="live Copilot test — set CHIEF_COPILOT_LIVE=1 (needs a Copilot login)",
)

_TOOL_NAME = "chief_probe_delete"


@define_tool(name=_TOOL_NAME, description="Delete the probe record (gate probe).")
def _probe_delete() -> str:
    return "probe record deleted"


@pytest.mark.timeout(120)  # live runtime spawn + tool round-trip; override the 30s cap
async def test_live_custom_tool_routes_through_the_gate() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    audit = RecordingAudit()
    policy = PolicyStore(session_factory, audit=audit)
    # Seed the tool APPROVED so the gate ALLOWs it with no card — the turn must not
    # block on an owner tap. Gating is still proven: the call round-trips the gate.
    await policy.seed(approved=[(_TOOL_NAME, None)])
    approvals = ApprovalManager(
        session_factory=session_factory, io=FakeIO(), policy=policy, audit=audit
    )
    can_use = build_can_use_tool(
        task_id=1,
        thread_key="live",
        tier="owner",
        route="live",
        policy=policy,
        approvals=approvals,
        audit=audit,
    )
    hook: HookCallback = build_pretool_hook(
        thread_key="live", tier="owner", policy=policy, audit=audit
    )

    seen = {"permission": False, "hook": False}
    permission_handler = build_permission_handler(can_use)
    base_hooks = build_session_hooks({"PreToolUse": [HookMatcher(hooks=[hook])]})
    assert base_hooks is not None
    base_pre_tool_use = base_hooks["on_pre_tool_use"]

    async def spy_permission(
        request: PermissionRequest, invocation: dict[str, str]
    ) -> PermissionRequestResult:
        if getattr(request, "tool_name", None) == _TOOL_NAME:
            seen["permission"] = True
        return await permission_handler(request, invocation)

    async def spy_hook(
        hook_input: PreToolUseHookInput, invocation: dict[str, str]
    ) -> PreToolUseHookOutput | None:
        if hook_input.get("toolName") == _TOOL_NAME:
            seen["hook"] = True
        output = base_pre_tool_use(hook_input, invocation)
        if asyncio.iscoroutine(output):
            output = await output
        return cast(PreToolUseHookOutput | None, output)

    spied_hooks: SessionHooks = {"on_pre_tool_use": spy_hook}
    client = CopilotClient()
    await client.start()
    try:
        session = await client.create_session(
            model=os.environ.get("CHIEF_COPILOT_MODEL", "auto"),
            tools=[_probe_delete],
            on_permission_request=spy_permission,
            hooks=spied_hooks,
        )
        idle = _idle_waiter(session)
        await session.send(
            f"Call the {_TOOL_NAME} tool now to delete the probe record. "
            "Use the tool; do not just describe it."
        )
        await idle
    finally:
        await client.stop()
    await engine.dispose()

    assert seen["permission"] or seen["hook"], (
        "the Copilot runtime routed the @define_tool call through NEITHER the "
        "permission handler nor the pre-tool hook — custom tools bypass the gate; "
        "route custom-tool calls through a gate-governed path before trusting this "
        "backend."
    )


def _idle_waiter(session: CopilotSession) -> "asyncio.Future[None]":
    """Future that resolves when the session goes idle (the turn boundary)."""
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def on_event(event: SessionEvent) -> None:
        if isinstance(event.data, SessionIdleData) and not done.done():
            done.set_result(None)

    session.on(on_event)
    return done
