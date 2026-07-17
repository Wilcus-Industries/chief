"""Cron: pure timing math and the live schedule loop."""

import asyncio
from datetime import UTC, datetime, time

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.tools import ToolContext, ToolRegistry
from chief.cron.service import CronService
from chief.cron.timing import defer_quiet, next_fire, parse_quiet_hours
from chief.cron.tools import register_cron_tools
from chief.persistence.db import make_session_factory
from chief.provider.base import ToolCall

NOON = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_every_spec_adds_seconds() -> None:
    assert next_fire("@every 90", NOON, None) == datetime(
        2026, 7, 15, 12, 1, 30, tzinfo=UTC
    )


def test_cron_spec_uses_croniter() -> None:
    assert next_fire("0 9 * * *", NOON, None) == datetime(
        2026, 7, 16, 9, 0, tzinfo=UTC
    )


def test_parse_quiet_hours() -> None:
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("23:00-08:00") == (time(23, 0), time(8, 0))


def test_quiet_hours_defer_same_day_window() -> None:
    quiet = parse_quiet_hours("11:00-13:00")
    assert defer_quiet(NOON, quiet) == NOON.replace(hour=13, minute=0)
    afternoon = NOON.replace(hour=15)
    assert defer_quiet(afternoon, quiet) == afternoon


def test_quiet_hours_defer_overnight_window() -> None:
    quiet = parse_quiet_hours("23:00-08:00")
    late = NOON.replace(hour=23, minute=30)
    assert defer_quiet(late, quiet) == datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    early = NOON.replace(hour=6)
    assert defer_quiet(early, quiet) == NOON.replace(hour=8, minute=0)
    assert defer_quiet(NOON, quiet) == NOON


class WakeSink:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.fired = asyncio.Event()

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)
        self.fired.set()


async def test_interval_schedule_fires_and_wakes(engine: AsyncEngine) -> None:
    wake = WakeSink()
    service = CronService(
        make_session_factory(engine), wake, quiet=None, poll_seconds=0.02
    )
    schedule_id = await service.create(
        description="tiny errand",
        spec="@every 0.05",
        wake_channel="cli",
        wake_thread="cli:home",
        prompt="do the thing",
    )
    service.start()
    try:
        await asyncio.wait_for(wake.fired.wait(), timeout=2)
    finally:
        await service.stop()
    woken = wake.messages[0]
    assert woken.sender == "system"
    assert woken.thread_key == "cli:home"
    assert f"#{schedule_id}" in woken.text
    assert "do the thing" in woken.text


async def test_deleted_schedule_stops_firing(engine: AsyncEngine) -> None:
    wake = WakeSink()
    service = CronService(
        make_session_factory(engine), wake, quiet=None, poll_seconds=0.02
    )
    schedule_id = await service.create(
        description="gone soon",
        spec="@every 0.03",
        wake_channel="cli",
        wake_thread="cli:home",
        prompt="x",
    )
    assert await service.delete(schedule_id) is True
    service.start()
    await asyncio.sleep(0.15)
    await service.stop()
    assert wake.messages == []


async def test_schedule_tool_create_list_delete(engine: AsyncEngine) -> None:
    service = CronService(
        make_session_factory(engine), WakeSink(), quiet=None, poll_seconds=0.02
    )
    registry = ToolRegistry()
    register_cron_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")

    created = await registry.dispatch(
        ToolCall(
            id="1",
            name="schedule",
            arguments={
                "action": "create",
                "description": "daily standup",
                "spec": "0 9 * * *",
                "prompt": "post standup",
            },
        ),
        context,
    )
    assert created == "schedule #1 created"
    listing = await registry.dispatch(
        ToolCall(id="2", name="schedule", arguments={"action": "list"})
    )
    assert "daily standup" in listing
    assert "cli:home" in listing
    incomplete = await registry.dispatch(
        ToolCall(
            id="3",
            name="schedule",
            arguments={"action": "create", "description": "x"},
        ),
        context,
    )
    assert incomplete.startswith("error: create needs")
    deleted = await registry.dispatch(
        ToolCall(
            id="4", name="schedule", arguments={"action": "delete", "schedule_id": 1}
        )
    )
    assert deleted == "schedule #1 deleted"
    assert await registry.dispatch(
        ToolCall(id="5", name="schedule", arguments={"action": "list"})
    ) == "no schedules"
