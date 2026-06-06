"""chief's own schedule tools (chief.tools.schedule): benign server + gated bash server.

Determinism: a fixed ``now`` (12:00 EDT) and owner_tz America/New_York frame the
next-fire math, so the computed UTC timestamps are exact.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import schedules as repo
from chief.tools.schedule import ScheduleBashService, ScheduleService

NOW = datetime(2026, 6, 4, 16, 0, tzinfo=UTC)  # 12:00 EDT


def _svc(session_factory: async_sessionmaker[AsyncSession]) -> ScheduleService:
    return ScheduleService(
        session_factory=session_factory, owner_tz="America/New_York", now=lambda: NOW
    )


def _bash(session_factory: async_sessionmaker[AsyncSession]) -> ScheduleBashService:
    return ScheduleBashService(
        session_factory=session_factory, owner_tz="America/New_York", now=lambda: NOW
    )


def test_servers_split_and_bash_is_off_the_benign_list() -> None:
    svc = ScheduleService(session_factory=None, owner_tz="UTC")  # type: ignore[arg-type]
    assert svc.server_name == "chief_schedule"
    assert set(svc.tool_names) == {
        "mcp__chief_schedule__schedule_once",
        "mcp__chief_schedule__schedule_recurring",
        "mcp__chief_schedule__create_monitor",
        "mcp__chief_schedule__list_schedules",
        "mcp__chief_schedule__cancel_schedule",
    }
    bash = ScheduleBashService(session_factory=None, owner_tz="UTC")  # type: ignore[arg-type]
    assert bash.server_name == "chief_schedule_bash"
    assert bash.tool_name == "mcp__chief_schedule_bash__schedule_bash"
    assert set(bash.tool_names) == {
        "mcp__chief_schedule_bash__schedule_bash",
        "mcp__chief_schedule_bash__create_monitor",
    }
    # Security: the gated bash tools must never appear on the benign allow-list, or
    # setting up an unattended ungated shell run would skip its approval card. The
    # benign create_monitor and the gated one share a bare name but differ by server.
    assert not set(bash.tool_names) & set(svc.tool_names)


async def test_schedule_once_creates_message_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_once().handler(
        {"when": "2026-06-04T13:00:00", "action": "stretch"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        rows = await repo.list_enabled(s)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == repo.KIND_ONCE
    assert row.action == "stretch"
    assert row.action_type == repo.ACTION_MESSAGE
    assert row.thread_key is None
    assert row.urgent is False
    # 13:00 EDT → 17:00 UTC.
    assert row.next_run is not None
    assert row.next_run.replace(tzinfo=UTC) == datetime(2026, 6, 4, 17, 0, tzinfo=UTC)


async def test_schedule_once_wakeup_with_target_and_urgent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_once().handler(
        {
            "when": "2026-06-04T13:00:00",
            "action": "daily brief",
            "action_type": "wakeup",
            "thread_key": "-100:7",
            "urgent": True,
        }
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_enabled(s))[0]
    assert row.action_type == repo.ACTION_WAKEUP
    assert row.thread_key == "-100:7"
    assert row.urgent is True


async def test_schedule_once_rejects_past_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_once().handler(
        {"when": "2026-06-04T06:00:00", "action": "too late"}  # 06:00 EDT < now
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []


async def test_schedule_once_rejects_unparseable_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_once().handler(
        {"when": "next tuesday", "action": "x"}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []


async def test_benign_once_refuses_bash_action_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The benign server must not be able to mint a bash schedule (that bypasses the
    # per-run gate) — only the gated chief_schedule_bash tool may.
    out = await _svc(session_factory)._build_once().handler(
        {"when": "2026-06-04T13:00:00", "action": "rm -rf /", "action_type": "bash"}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []


async def test_schedule_recurring_creates_row_and_computes_next(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_recurring().handler(
        {"cron": "0 9 * * *", "action": "morning"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_enabled(s))[0]
    assert row.kind == repo.KIND_RECURRING
    assert row.spec == "0 9 * * *"
    assert row.action_type == repo.ACTION_MESSAGE
    # Next 09:00 EDT after 12:00 EDT Jun 4 → Jun 5 09:00 EDT = 13:00 UTC.
    assert row.next_run is not None
    assert row.next_run.replace(tzinfo=UTC) == datetime(2026, 6, 5, 13, 0, tzinfo=UTC)


async def test_schedule_recurring_wakeup_registers_morning_brief(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The setup-morning-brief skill drives exactly this: a daily recurring *wakeup* (not
    # the default message), so each morning boots a full agent turn to write the brief.
    out = await _svc(session_factory)._build_recurring().handler(
        {
            "cron": "0 7 * * *",
            "action": "Assemble and send the owner's morning brief.",
            "action_type": "wakeup",
            "thread_key": "-100:7",
        }
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_enabled(s))[0]
    assert row.kind == repo.KIND_RECURRING
    assert row.spec == "0 7 * * *"
    assert row.action_type == repo.ACTION_WAKEUP
    assert row.thread_key == "-100:7"
    # Next 07:00 EDT after 12:00 EDT Jun 4 → Jun 5 07:00 EDT = 11:00 UTC.
    assert row.next_run is not None
    assert row.next_run.replace(tzinfo=UTC) == datetime(2026, 6, 5, 11, 0, tzinfo=UTC)


async def test_schedule_recurring_rejects_bad_cron(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_recurring().handler(
        {"cron": "not a cron", "action": "x"}
    )
    assert out["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []


async def test_list_schedules_shows_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_once().handler(
        {"when": "2026-06-04T13:00:00", "action": "stretch"}
    )
    out = await svc._build_list().handler({})
    assert out["is_error"] is False
    assert "stretch" in out["content"][0]["text"]


async def test_list_schedules_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_list().handler({})
    assert out["is_error"] is False


async def test_cancel_schedule_disables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _svc(session_factory)
    await svc._build_once().handler(
        {"when": "2026-06-04T13:00:00", "action": "stretch"}
    )
    async with session_factory() as s:
        sid = (await repo.list_enabled(s))[0].id
    out = await svc._build_cancel().handler({"schedule_id": sid})
    assert out["is_error"] is False
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []


async def test_cancel_unknown_id_is_an_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _svc(session_factory)._build_cancel().handler({"schedule_id": 999})
    assert out["is_error"] is True


async def test_schedule_bash_creates_recurring_bash_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _bash(session_factory)._build_tool().handler(
        {"command": "df -h", "cron": "0 2 * * *"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_enabled(s))[0]
    assert row.action_type == repo.ACTION_BASH
    assert row.kind == repo.KIND_RECURRING
    assert row.action == "df -h"
    assert row.spec == "0 2 * * *"


async def test_schedule_bash_one_off_with_when(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await _bash(session_factory)._build_tool().handler(
        {"command": "backup.sh", "when": "2026-06-04T13:00:00"}
    )
    assert out["is_error"] is False
    async with session_factory() as s:
        row = (await repo.list_enabled(s))[0]
    assert row.kind == repo.KIND_ONCE
    assert row.action_type == repo.ACTION_BASH


async def test_schedule_bash_requires_exactly_one_of_cron_or_when(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = _bash(session_factory)
    neither = await svc._build_tool().handler({"command": "x"})
    both = await svc._build_tool().handler(
        {"command": "x", "cron": "0 2 * * *", "when": "2026-06-04T13:00:00"}
    )
    assert neither["is_error"] is True
    assert both["is_error"] is True
    async with session_factory() as s:
        assert await repo.list_enabled(s) == []
