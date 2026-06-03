"""Approval + policy repositories (the M3 gate's persistence layer)."""

from sqlalchemy.ext.asyncio import AsyncSession

from chief.persistence import approvals as appr_repo
from chief.persistence import policy as policy_repo

# ---- approvals ---------------------------------------------------------------


async def test_create_approval_starts_requested(db_session: AsyncSession) -> None:
    approval = await appr_repo.create_approval(
        db_session, task_id=None, kind="Bash", payload_preview="Run: ls"
    )

    assert approval.id is not None
    assert approval.state == appr_repo.REQUESTED
    assert approval.payload_preview == "Run: ls"
    assert approval.decided_at is None


async def test_set_state_terminal_stamps_decider(db_session: AsyncSession) -> None:
    approval = await appr_repo.create_approval(db_session, task_id=None, kind="Bash")

    await appr_repo.set_state(
        db_session, approval, appr_repo.APPROVED, decided_by="42"
    )

    reloaded = await appr_repo.get(db_session, approval.id)
    assert reloaded is not None
    assert reloaded.state == appr_repo.APPROVED
    assert reloaded.decided_by == "42"
    assert reloaded.decided_at is not None


async def test_set_state_nonterminal_leaves_decider_unset(
    db_session: AsyncSession,
) -> None:
    approval = await appr_repo.create_approval(db_session, task_id=None, kind="Bash")

    await appr_repo.set_state(db_session, approval, appr_repo.NOTIFIED)

    reloaded = await appr_repo.get(db_session, approval.id)
    assert reloaded is not None
    assert reloaded.state == appr_repo.NOTIFIED
    assert reloaded.decided_at is None


async def test_list_pending_only_undecided_oldest_first(
    db_session: AsyncSession,
) -> None:
    first = await appr_repo.create_approval(db_session, task_id=None, kind="Bash")
    second = await appr_repo.create_approval(db_session, task_id=None, kind="Bash")
    decided = await appr_repo.create_approval(db_session, task_id=None, kind="Bash")
    await appr_repo.set_state(db_session, second, appr_repo.NOTIFIED)
    await appr_repo.set_state(db_session, decided, appr_repo.DENIED, decided_by="42")

    pending = await appr_repo.list_pending(db_session)

    assert [a.id for a in pending] == [first.id, second.id]


async def test_get_missing_returns_none(db_session: AsyncSession) -> None:
    assert await appr_repo.get(db_session, 9999) is None


# ---- policy ------------------------------------------------------------------


async def test_add_entry_inserts_and_lists(db_session: AsyncSession) -> None:
    entry = await policy_repo.add_entry(
        db_session, list_name=policy_repo.APPROVED, tool="Bash", arg_pattern="ls"
    )

    assert entry is not None
    rows = await policy_repo.list_entries(db_session, policy_repo.APPROVED)
    assert [(r.tool, r.arg_pattern) for r in rows] == [("Bash", "ls")]


async def test_add_entry_is_idempotent(db_session: AsyncSession) -> None:
    first = await policy_repo.add_entry(
        db_session, list_name=policy_repo.NEVER, tool="Bash", arg_pattern="rm"
    )
    dup = await policy_repo.add_entry(
        db_session, list_name=policy_repo.NEVER, tool="Bash", arg_pattern="rm"
    )

    assert first is not None
    assert dup is None
    rows = await policy_repo.list_entries(db_session, policy_repo.NEVER)
    assert len(rows) == 1


async def test_entry_exists_handles_null_arg_pattern(
    db_session: AsyncSession,
) -> None:
    await policy_repo.add_entry(
        db_session, list_name=policy_repo.NEVER, tool="WebFetch", arg_pattern=None
    )

    assert await policy_repo.entry_exists(
        db_session, list_name=policy_repo.NEVER, tool="WebFetch", arg_pattern=None
    )
    assert not await policy_repo.entry_exists(
        db_session, list_name=policy_repo.NEVER, tool="WebFetch", arg_pattern="x"
    )
