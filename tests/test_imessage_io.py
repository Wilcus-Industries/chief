"""iMessage outbound (#156): send argv, cards-as-text, draft-first, mirroring.

The outbound central mechanism — the OS send path — is asserted at the exact
subprocess seam: every test checks the fixed JXA scripts and the argv the runner
receives (data only ever travels as argv, never spliced into script text). The
draft-first round trip runs against the REAL :class:`ApprovalManager` (park,
resolve, deliver) with only the osascript child faked.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import AdmissionCard, BudgetCard
from chief.adapters.imessage import (
    IMESSAGE_LIMIT,
    SEND_FILE_SCRIPT,
    SEND_TEXT_SCRIPT,
    DraftFirstIO,
    IMessageTaskIO,
)
from chief.adapters.mirror import MirrorTaskIO
from chief.gate.approvals import (
    DRAFT_SEND_KIND,
    ApprovalAction,
    ApprovalCard,
    ApprovalManager,
)
from chief.gate.policy import PolicyStore
from chief.obs.audit import AuditLog
from chief.persistence import imessage as imessage_repo
from chief.persistence.approvals import list_pending
from chief.persistence.messages import ROLE_CHIEF, MessageLog
from chief.persistence.models import MessageLogEntry, PolicyEntry
from chief.tools.apple.runner import OSASCRIPT_PATH, ScriptResult
from imessage_helpers import FixtureRunner

OWNER = "+15550000001"
MOM = "+15550000002"


def _jxa_argv(script: str, *args: str) -> tuple[str, ...]:
    return (OSASCRIPT_PATH, "-l", "JavaScript", "-e", script, *args)


# ---- IMessageTaskIO ----------------------------------------------------------------


async def test_send_generates_the_exact_jxa_invocation(tmp_path: Path) -> None:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))

    await io.send(MOM, "hello from chief")

    assert runner.jxa_calls == [
        _jxa_argv(SEND_TEXT_SCRIPT, MOM, "hello from chief")
    ]


async def test_send_splits_long_text_at_the_imessage_limit(tmp_path: Path) -> None:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))

    await io.send(MOM, "word " * 1500)  # 7500 chars > IMESSAGE_LIMIT

    assert len(runner.jxa_calls) == 2
    assert all(len(call[-1]) <= IMESSAGE_LIMIT for call in runner.jxa_calls)


async def test_send_failure_raises_instead_of_swallowing(tmp_path: Path) -> None:
    runner = FixtureRunner(
        ScriptResult(
            "", "execution error: Not authorized to send Apple events. (-1743)", 1
        )
    )
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))

    with pytest.raises(RuntimeError, match="send to"):
        await io.send(MOM, "hi")


async def test_send_file_writes_the_outbox_and_sends_the_path(
    tmp_path: Path,
) -> None:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))

    await io.send_file(MOM, "reply.md", b"# hi", caption="the full reply")

    outbox_file = tmp_path / "outbox" / "reply.md"
    assert outbox_file.read_bytes() == b"# hi"
    assert runner.jxa_calls[0] == _jxa_argv(
        SEND_FILE_SCRIPT, MOM, str(outbox_file.resolve())
    )
    assert runner.jxa_calls[1][-2:] == (MOM, "the full reply")


async def test_cards_render_as_text_with_an_answer_hint(tmp_path: Path) -> None:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))

    ref = await io.send_card(OWNER, ApprovalCard(approval_id=7, text="Run: rm x"))
    await io.edit_card(ref, "✅ Approved (once) — by web")
    await io.send_admission_card(
        OWNER, AdmissionCard(contact_id=3, text="New contact: Mom.")
    )
    await io.send_budget_card(OWNER, BudgetCard(cycle="2026-07", text="Cap hit."))

    assert ref == OWNER  # no edit API — the outcome posts as a follow-up text
    texts = [call[-1] for call in runner.jxa_calls]
    assert "Run: rm x" in texts[0] and "web UI" in texts[0]
    assert texts[1] == "✅ Approved (once) — by web"
    assert "New contact: Mom." in texts[2]
    assert "Cap hit." in texts[3]


async def test_thread_lifecycle_is_flat(tmp_path: Path) -> None:
    io = IMessageTaskIO(FixtureRunner(), outbox_dir=str(tmp_path / "outbox"))

    assert await io.create_thread(like_thread_key=MOM, title="ignored") == MOM
    await io.archive_thread(MOM)  # no-op, must not call out


# ---- mirror wrapping ---------------------------------------------------------------


async def test_mirrored_send_delivers_then_records_the_message_log_row(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))
    mirror = MirrorTaskIO(
        io, platform="imessage", log=MessageLog(session_factory), server=None
    )

    await mirror.send(MOM, "mirrored reply")

    assert runner.jxa_calls[0][-2:] == (MOM, "mirrored reply")
    async with session_factory() as session:
        rows = list((await session.execute(select(MessageLogEntry))).scalars())
    assert [(r.platform, r.thread_key, r.role, r.text) for r in rows] == [
        ("imessage", MOM, ROLE_CHIEF, "mirrored reply")
    ]


# ---- draft-first -------------------------------------------------------------------


def _manager(
    session_factory: async_sessionmaker[AsyncSession],
    io: object,
    tmp_path: Path,
    *,
    timeout: float = 5.0,
) -> ApprovalManager:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return ApprovalManager(
        session_factory=session_factory,
        io=io,  # type: ignore[arg-type]
        policy=PolicyStore(session_factory, audit=audit),
        audit=audit,
        timeout_seconds=timeout,
    )


def _draft_stack(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    timeout: float = 5.0,
) -> tuple[DraftFirstIO, FixtureRunner, ApprovalManager]:
    runner = FixtureRunner()
    io = IMessageTaskIO(runner, outbox_dir=str(tmp_path / "outbox"))
    mirror = MirrorTaskIO(
        io, platform="imessage", log=MessageLog(session_factory), server=None
    )
    manager = _manager(session_factory, mirror, tmp_path, timeout=timeout)
    draft_io = DraftFirstIO(
        mirror,
        approvals=manager,
        session_factory=session_factory,
        front_desk=OWNER,
    )
    return draft_io, runner, manager


async def _decide(
    session_factory: async_sessionmaker[AsyncSession],
    manager: ApprovalManager,
    action: ApprovalAction,
) -> None:
    """Wait for the parked card's committed row, then decide it (STYLEGUIDE: sync
    on the write you assert — the approval row is committed before the card posts)."""
    for _ in range(100):
        async with session_factory() as session:
            pending = await list_pending(session)
        if pending:
            assert await manager.resolve(
                pending[0].id, action, decided_by="test"
            )
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no approval card was parked")


async def test_draft_mode_parks_the_outbound_and_approve_sends_it(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle=MOM, tier="guest", mode=imessage_repo.MODE_DRAFT
        )
        await imessage_repo.mark_contacted(session, MOM)
    draft_io, runner, manager = _draft_stack(session_factory, tmp_path)

    send = asyncio.create_task(draft_io.send(MOM, "See you at 6."))
    await _decide(session_factory, manager, ApprovalAction.APPROVE_ONCE)
    await send

    sent_to_mom = [c for c in runner.jxa_calls if c[-2] == MOM]
    assert [c[-1] for c in sent_to_mom] == ["See you at 6."]
    # The card itself texted the owner's Front Desk (and mirrors onto the socket).
    card_texts = [c[-1] for c in runner.jxa_calls if c[-2] == OWNER]
    assert any("See you at 6." in text for text in card_texts)


async def test_denied_draft_is_never_delivered_or_logged(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle=MOM, tier="guest", mode=imessage_repo.MODE_DRAFT
        )
        await imessage_repo.mark_contacted(session, MOM)
    draft_io, runner, manager = _draft_stack(session_factory, tmp_path)

    send = asyncio.create_task(draft_io.send(MOM, "Sure, 123 Main St."))
    await _decide(session_factory, manager, ApprovalAction.DENY_ONCE)
    await send

    assert [c for c in runner.jxa_calls if c[-2] == MOM] == []  # killed clean
    async with session_factory() as session:
        rows = list((await session.execute(select(MessageLogEntry))).scalars())
    # Nothing to MOM in the log either — a killed draft never happened.
    assert all(row.thread_key != MOM for row in rows)


async def test_first_send_to_a_new_handle_cards_regardless_of_auto_mode(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle=MOM, tier="guest", mode=imessage_repo.MODE_AUTO
        )
    draft_io, runner, manager = _draft_stack(session_factory, tmp_path)

    send = asyncio.create_task(draft_io.send(MOM, "Hi, this is chief."))
    await _decide(session_factory, manager, ApprovalAction.APPROVE_ONCE)
    await send

    assert [c[-1] for c in runner.jxa_calls if c[-2] == MOM] == [
        "Hi, this is chief."
    ]
    # The approval marked the handle contacted: the next auto send flows free.
    await draft_io.send(MOM, "Second message.")
    assert [c[-1] for c in runner.jxa_calls if c[-2] == MOM] == [
        "Hi, this is chief.",
        "Second message.",
    ]


async def test_owner_directed_sends_never_draft_gate(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(session, handle=OWNER, tier="owner")
    draft_io, runner, _ = _draft_stack(session_factory, tmp_path)

    await draft_io.send(OWNER, "Morning brief ready.")  # returns without a card

    assert runner.jxa_calls[0][-2:] == (OWNER, "Morning brief ready.")


async def test_always_allow_on_a_draft_card_writes_no_tool_policy_rule(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle=MOM, tier="guest", mode=imessage_repo.MODE_DRAFT
        )
        await imessage_repo.mark_contacted(session, MOM)
    draft_io, _, manager = _draft_stack(session_factory, tmp_path)

    send = asyncio.create_task(draft_io.send(MOM, "ok"))
    await _decide(session_factory, manager, ApprovalAction.ALWAYS_ALLOW)
    await send

    async with session_factory() as session:
        rules = list((await session.execute(select(PolicyEntry))).scalars())
    assert rules == []  # DRAFT_SEND_KIND is a pseudo-kind, never a policy rule
    assert DRAFT_SEND_KIND == "DraftSend"
