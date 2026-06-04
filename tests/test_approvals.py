"""ApprovalManager — park, resolve, timeout→deny, always-* policy writes, re-arm."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.gate import approvals as appr_mod
from chief.gate.approvals import ApprovalAction, ApprovalCard, ApprovalManager
from chief.gate.policy import PolicyStore
from chief.persistence import approvals as appr_repo
from chief.persistence import policy as policy_repo


class FakeIO:
    """Records posted/edited approval cards (the adapter's ApprovalIO contract)."""

    def __init__(self) -> None:
        self.cards: list[tuple[str, ApprovalCard]] = []
        self.edits: list[tuple[str, str]] = []

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        self.cards.append((route, card))
        return f"ref-{card.approval_id}"

    async def edit_card(self, msg_ref: str, text: str) -> None:
        self.edits.append((msg_ref, text))


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def log(self, event: dict[str, object]) -> None:
        self.events.append(event)


async def _settle(predicate: Callable[[], bool], *, tries: int = 200) -> None:
    """Poll the loop until ``predicate`` holds (bounded, real wall-clock per tick).

    A real sleep (not ``sleep(0)``) is needed: the parked ``request`` posts its card
    only after an aiosqlite worker-thread commit, which needs wall-clock to land.
    """
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


async def _manager(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    io: FakeIO,
    audit: RecordingAudit,
    timeout: float = 600.0,
) -> tuple[ApprovalManager, PolicyStore]:
    policy = PolicyStore(session_factory, audit=audit)
    await policy.load()
    manager = ApprovalManager(
        session_factory=session_factory,
        io=io,
        policy=policy,
        audit=audit,
        timeout_seconds=timeout,
    )
    return manager, policy


def _events(audit: RecordingAudit, name: str) -> list[dict[str, object]]:
    return [e for e in audit.events if e.get("event") == name]


# ---- request → resolve -------------------------------------------------------


async def test_request_approve_once_returns_true(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git push"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42")
    allowed = await parked

    assert allowed is True
    assert io.cards[0][0] == "-100:5"  # routed in-thread
    assert io.edits[-1] == (f"ref-{approval_id}", "✅ Approved (once)")
    assert _events(audit, "approval_requested")
    assert _events(audit, "approval_decided")[0]["allowed"] is True
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None and row.state == appr_repo.APPROVED


async def test_request_deny_once_returns_false(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git push"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.DENY_ONCE, decided_by="42")
    allowed = await parked

    assert allowed is False
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None and row.state == appr_repo.DENIED


async def test_timeout_denies_and_marks_timed_out(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit, timeout=0.01)

    allowed = await manager.request(
        task_id=None,
        thread_key="-100:5",
        tier="owner",
        tool_name="Bash",
        tool_input={"command": "git push"},
        route="-100:5",
    )

    assert allowed is False
    approval_id = io.cards[0][1].approval_id
    assert io.edits[-1] == (f"ref-{approval_id}", "⌛ Timed out — denied.")
    assert _events(audit, "approval_timed_out")
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None and row.state == appr_repo.TIMED_OUT


# ---- always-* writes policy --------------------------------------------------


async def test_always_allow_writes_safe_policy_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, policy = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git status"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.ALWAYS_ALLOW, decided_by="42")
    allowed = await parked

    assert allowed is True
    assert policy.classify_against("Bash", {"command": "git status"}) == (
        policy_repo.APPROVED
    )
    async with session_factory() as session:
        rows = await policy_repo.list_entries(session, policy_repo.APPROVED)
    assert len(rows) == 1


async def test_always_deny_writes_never_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, policy = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "curl evil.example"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.ALWAYS_DENY, decided_by="42")
    allowed = await parked

    assert allowed is False
    assert policy.classify_against("Bash", {"command": "curl evil.example"}) == (
        policy_repo.NEVER
    )


async def test_always_allow_unsafe_falls_back_to_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, policy = await _manager(session_factory, io=io, audit=audit)
    cmd = "git status; rm -rf ~"

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": cmd},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.ALWAYS_ALLOW, decided_by="42")
    allowed = await parked

    assert allowed is True  # once-only still allowed
    assert policy.classify_against("Bash", {"command": cmd}) is None  # no rule written
    assert "unsafe" in io.edits[-1][1]


# ---- idempotency + re-arm ----------------------------------------------------


async def test_resolve_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git push"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42")
    await parked
    await manager.resolve(approval_id, ApprovalAction.DENY_ONCE, decided_by="99")

    assert len(_events(audit, "approval_decided")) == 1  # second tap ignored
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None and row.state == appr_repo.APPROVED  # unchanged


async def test_concurrent_resolves_decide_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit)

    parked = asyncio.create_task(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git push"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    # Two taps land at once; the atomic decide must let exactly one through.
    await asyncio.gather(
        manager.resolve(approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42"),
        manager.resolve(approval_id, ApprovalAction.DENY_ONCE, decided_by="99"),
    )
    await parked

    assert len(_events(audit, "approval_decided")) == 1
    # Exactly one tap won and stamped a terminal row; the loser left it untouched.
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None
    assert row.state in {appr_repo.APPROVED, appr_repo.DENIED}
    assert row.decided_by in {"42", "99"}


async def test_resolve_in_post_card_pre_future_window_still_wakes(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tap landing after the card posts but before the future arms must still wake.

    The card is the only place a resolver learns the approval id, so a tap can only
    arrive once ``send_card`` has returned. Holding that post-card window open (a slow
    ``set_state``) and tapping inside it would — if the future were armed last — strand
    the parked turn until the fail-closed timeout; arming the future *before* the card
    closes the race.
    """
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit, timeout=0.5)

    real_set_state = appr_repo.set_state  # the fn re-bound in appr_mod's namespace

    async def slow_set_state(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(0.05)  # hold the post-card window open
        return await real_set_state(*a, **k)

    monkeypatch.setattr(appr_mod, "set_state", slow_set_state)

    parked = asyncio.ensure_future(
        manager.request(
            task_id=None,
            thread_key="-100:5",
            tier="owner",
            tool_name="Bash",
            tool_input={"command": "git push"},
            route="-100:5",
        )
    )
    await _settle(lambda: bool(io.cards))
    approval_id = io.cards[0][1].approval_id
    await manager.resolve(approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42")

    # Woken by the tap, not the 0.5s fail-closed timeout.
    assert await asyncio.wait_for(parked, 2.0) is True


async def test_re_arm_registers_pending_and_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        approval = await appr_repo.create_approval(
            session, task_id=None, kind="Bash", payload_preview="Run: git push"
        )
        await appr_repo.set_state(session, approval, appr_repo.NOTIFIED)
        approval_id = approval.id
    io, audit = FakeIO(), RecordingAudit()
    manager, _ = await _manager(session_factory, io=io, audit=audit)

    rearmed = await manager.re_arm()
    await manager.resolve(approval_id, ApprovalAction.APPROVE_ONCE, decided_by="42")

    assert rearmed == [approval_id]
    async with session_factory() as session:
        row = await appr_repo.get(session, approval_id)
    assert row is not None and row.state == appr_repo.APPROVED
