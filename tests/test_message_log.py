"""The #132 message-log repo — record both directions + claim-replay on attach.

Every test runs the real repo over the in-memory ``session_factory`` fixture; nothing
is mocked. The claim is the central mechanism: it fetches undelivered outbound rows,
marks them all delivered in one transaction, and returns the most-recent window of
decoded frames in id order — so a second reattach re-delivers nothing.
"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence.messages import ROLE_CHIEF, ROLE_OWNER, MessageLog
from chief.persistence.models import MessageLogEntry


def _reply_payload(text: str) -> str:
    return json.dumps({"type": "reply", "thread_key": "cli:main", "text": text})


async def test_record_persists_role_surface_kind_and_timestamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_OWNER, surface="dm",
        kind="user", text="hi", payload=None, delivered=True,
    )
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
        kind="reply", text="reply:hi", payload=_reply_payload("reply:hi"),
        delivered=False,
    )

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(MessageLogEntry).order_by(MessageLogEntry.id)
            )
        ).scalars().all()

    assert len(rows) == 2
    inbound, outbound = rows
    assert inbound.role == ROLE_OWNER and inbound.kind == "user"
    assert inbound.surface == "dm" and inbound.payload is None
    assert inbound.delivered is True and inbound.created_at is not None
    assert outbound.role == ROLE_CHIEF and outbound.kind == "reply"
    assert outbound.delivered is False and outbound.created_at is not None


async def test_claim_replay_returns_last_n_undelivered_in_id_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    for i in range(5):
        await log.record(
            platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
            kind="reply", text=str(i), payload=_reply_payload(str(i)),
            delivered=False,
        )

    frames = await log.claim_replay(platform="cli", limit=3)

    assert [f["text"] for f in frames] == ["2", "3", "4"]


async def test_claim_replay_marks_all_undelivered_even_beyond_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    for i in range(5):
        await log.record(
            platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
            kind="reply", text=str(i), payload=_reply_payload(str(i)),
            delivered=False,
        )

    await log.claim_replay(platform="cli", limit=3)
    # Every undelivered row — including the two outside the window — is now delivered.
    assert await log.claim_replay(platform="cli", limit=3) == []


async def test_claim_replay_ignores_delivered_and_inbound_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
        kind="reply", text="already", payload=_reply_payload("already"),
        delivered=True,
    )
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_OWNER, surface="dm",
        kind="user", text="hi", payload=None, delivered=True,
    )

    assert await log.claim_replay(platform="cli") == []


async def test_claim_replay_skips_undelivered_row_with_no_payload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
        kind="reply", text="no-payload", payload=None, delivered=False,
    )

    # Defensive: an undelivered no-payload row yields no frame but is still claimed.
    assert await log.claim_replay(platform="cli") == []
    async with session_factory() as session:
        row = (
            await session.execute(select(MessageLogEntry))
        ).scalar_one()
    assert row.delivered is True
