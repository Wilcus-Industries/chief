"""chief's watch tools (chief.tools.watches): create/list/cancel (#165).

Determinism: a fixed ``now`` (12:00 EDT) and owner_tz America/New_York frame the
expiry math, mirroring test_tools_schedule.py.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import watches as repo
from chief.tools.watches import WatchService

NOW = datetime(2026, 6, 4, 16, 0, tzinfo=UTC)  # 12:00 EDT


def _svc(session_factory: async_sessionmaker[AsyncSession]) -> WatchService:
    return WatchService(
        session_factory=session_factory, owner_tz="America/New_York", now=lambda: NOW
    )


def test_tool_names_and_server_name() -> None:
    svc = WatchService(session_factory=None, owner_tz="UTC")  # type: ignore[arg-type]
    assert svc.server_name == "chief_watches"
    assert set(svc.tool_names) == {
        "mcp__chief_watches__create_watch",
        "mcp__chief_watches__list_watches",
        "mcp__chief_watches__cancel_watch",
        "mcp__chief_watches__list_watch_candidates",
        "mcp__chief_watches__confirm_watch_candidate",
    }


async def test_create_watch_defaults_to_14_day_ttl(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "tell mom I'm late"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.expiry.replace(tzinfo=UTC) == repo.default_expiry(NOW)
    assert row.tone == repo.TONE_REPORT
    assert row.target_handle == "+15550000001"
    assert row.instruction == "tell mom I'm late"


async def test_create_watch_with_local_expiry_stores_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "2026-06-04T20:00:00",  # local midnight-ish, 20:00 EDT
        }
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    # 20:00 EDT → 00:00 UTC (next day).
    assert row.expiry.replace(tzinfo=UTC) == datetime(2026, 6, 5, 0, 0, tzinfo=UTC)


async def test_create_watch_silent_tone_persisted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x", "tone": "silent"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.tone == repo.TONE_SILENT


async def test_create_watch_without_target_handle_creates_unbound_watch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#168: a blank/omitted target_handle is now a deliberate unbound watch —
    it no longer errors (intentional behavior change from #165)."""
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "", "instruction": "watch for the plumber"}
    )
    assert out["is_error"] is False
    assert "unknown sender" in out["content"][0]["text"]
    async with session_factory() as s:
        row = (await repo.list_watches(s))[0]
    assert row.target_handle is None
    assert row.instruction == "watch for the plumber"


async def test_create_watch_rejects_empty_instruction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": ""}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_bad_tone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x", "tone": "yell"}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_unparseable_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "next tuesday",
        }
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_create_watch_rejects_past_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_create().handler(
        {
            "target_handle": "+15550000001",
            "instruction": "x",
            "expiry": "2026-06-04T06:00:00",  # 06:00 EDT < now (12:00 EDT)
        }
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_watches(s) == []


async def test_list_watches_formats_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "tell her I'll be late"}
    )
    out = await svc._build_list().handler({})
    assert out["is_error"] is False
    text = out["content"][0]["text"]
    assert "+15550000001" in text
    assert "tell her I'll be late" in text
    assert "armed" in text
    assert "report" in text


async def test_list_watches_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_list().handler({})
    assert out["is_error"] is False
    assert "No watches" in out["content"][0]["text"]


async def test_cancel_watch_unknown_id_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_cancel().handler({"watch_id": 999})
    assert out["is_error"] is True


async def test_cancel_watch_armed_cancels_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x"}
    )
    async with session_factory() as s:
        watch_id = (await repo.list_watches(s))[0].id
    out = await svc._build_cancel().handler({"watch_id": watch_id})
    assert out["is_error"] is False
    assert "will never fire" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch_id)
    assert row is not None and row.state == repo.STATE_CANCELLED


async def test_cancel_watch_already_cancelled_is_a_no_op_with_clear_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {"target_handle": "+15550000001", "instruction": "x"}
    )
    async with session_factory() as s:
        watch_id = (await repo.list_watches(s))[0].id
    first = await svc._build_cancel().handler({"watch_id": watch_id})
    assert first["is_error"] is False
    second = await svc._build_cancel().handler({"watch_id": watch_id})
    assert second["is_error"] is False
    assert "nothing to cancel" in second["content"][0]["text"]


# ---- unknown-sender confirm flow (#168) -------------------------------------------


async def test_list_watch_candidates_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_list_candidates().handler({})
    assert out["is_error"] is False
    assert "No pending candidates" in out["content"][0]["text"]


async def test_list_watch_candidates_formats_pending_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_list_candidates().handler({})
    assert out["is_error"] is False
    text = out["content"][0]["text"]
    assert f"#{candidate.id}" in text
    assert "+15550000009" in text
    assert f"watch #{watch.id}" in text


async def test_confirm_watch_candidate_yes_binds_and_reports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "yes"}
    )
    assert out["is_error"] is False
    assert f"watch #{watch.id}" in out["content"][0]["text"]
    assert "+15550000009" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch.id)
    assert row is not None and row.target_handle == "+15550000009"


async def test_confirm_watch_candidate_no_rejects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler({"instruction": "watch for the plumber"})
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    out = await svc._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "no"}
    )
    assert out["is_error"] is False
    assert "stays inert" in out["content"][0]["text"]
    async with session_factory() as s:
        row = await repo.get_watch(s, watch.id)
    assert row is not None and row.target_handle is None


async def test_confirm_watch_candidate_unknown_id_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_confirm_candidate().handler(
        {"candidate_id": 999, "decision": "yes"}
    )
    assert out["is_error"] is True


async def test_confirm_watch_candidate_expired_watch_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_create().handler(
        {
            "instruction": "watch for the plumber",
            "expiry": "2026-06-04T13:00:00",  # 13:00 EDT, 1h after NOW (12:00 EDT)
        }
    )
    async with session_factory() as s:
        watch = (await repo.list_watches(s))[0]
        candidate = await repo.create_candidate(
            s, watch_id=watch.id, handle="+15550000009", first_seen=NOW
        )
    later = WatchService(
        session_factory=session_factory,
        owner_tz="America/New_York",
        now=lambda: datetime(2026, 6, 4, 18, 0, tzinfo=UTC),  # past the expiry
    )
    out = await later._build_confirm_candidate().handler(
        {"candidate_id": candidate.id, "decision": "yes"}
    )
    assert out["is_error"] is True
