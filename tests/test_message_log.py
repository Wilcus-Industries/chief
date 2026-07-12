"""The #132 message-log repo — record both directions + claim-replay on attach.

Every test runs the real repo over the per-test sqlite ``session_factory`` fixture;
nothing is mocked. The claim is the central mechanism: it fetches undelivered outbound
rows, marks them all delivered in one transaction, and returns the most-recent window of
decoded frames in id order — so a second reattach re-delivers nothing.

:meth:`MessageLog.history` (#134) is the read side: the bounded recent window a switch
backfills from, rendered off the row columns rather than the replay-only ``payload``.
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


async def test_history_returns_only_the_named_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    await log.record(
        platform="cli", thread_key="cli:main", role=ROLE_CHIEF, surface="dm",
        kind="reply", text="cli reply", payload=_reply_payload("cli reply"),
    )
    await log.record(
        platform="telegram", thread_key="-100:5", role=ROLE_CHIEF,
        kind="reply", text="tg-5 reply",
    )
    await log.record(
        platform="telegram", thread_key="-100:9", role=ROLE_CHIEF,
        kind="reply", text="tg-9 reply",
    )

    rows = await log.history(platform="telegram", thread_key="-100:5")

    assert [r["text"] for r in rows] == ["tg-5 reply"]


async def test_history_returns_oldest_first_both_roles_and_payload_less_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The regression guard: mirror rows (#133) and every inbound row have no stored
    # payload — a payload-based read would be empty for exactly these foreign rows.
    log = MessageLog(session_factory)
    await log.record(
        platform="telegram", thread_key="-100:5", role=ROLE_OWNER,
        kind="user", text="hello there", payload=None,
    )
    await log.record(
        platform="telegram", thread_key="-100:5", role=ROLE_CHIEF,
        kind="reply", text="hi back", payload=None,
    )

    rows = await log.history(platform="telegram", thread_key="-100:5")

    assert [r["text"] for r in rows] == ["hello there", "hi back"]
    assert [r["role"] for r in rows] == [ROLE_OWNER, ROLE_CHIEF]
    assert all(r["filename"] is None for r in rows)


async def test_history_is_bounded_to_the_most_recent_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    for i in range(12):
        await log.record(
            platform="cli", thread_key="cli:main", role=ROLE_CHIEF,
            kind="reply", text=str(i), payload=None,
        )

    rows = await log.history(platform="cli", thread_key="cli:main", limit=5)

    assert [r["text"] for r in rows] == ["7", "8", "9", "10", "11"]


async def test_history_file_row_surfaces_filename_and_no_bytes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    log = MessageLog(session_factory)
    await log.record(
        platform="telegram", thread_key="-100:5", role=ROLE_CHIEF,
        kind="file", text="a caption", filename="report.pdf", payload=None,
    )

    (row,) = await log.history(platform="telegram", thread_key="-100:5")

    assert row["kind"] == "file"
    assert row["filename"] == "report.pdf"
    assert "data" not in row and "bytes" not in row
