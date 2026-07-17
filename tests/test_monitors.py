"""Monitors: predicates, wakes, persistence, and the agent-facing tools."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.tools import ToolContext, ToolRegistry
from chief.bus import Event, EventBus
from chief.monitors.service import ModelJudge, MonitorService
from chief.monitors.tools import register_monitor_tools
from chief.persistence.db import make_session_factory
from chief.provider.base import ToolCall

from .fakes import FakeProvider, text_turn

CODE_PREDICATE = {"kind": "code", "field": "text", "pattern": "urgent"}


class WakeSink:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)


STRANGER = "+15559998888"


def inbound(text: str, channel: str = "cli", sender: str = STRANGER) -> Event:
    return Event(
        type="message.inbound",
        channel=channel,
        payload={"thread_key": f"{channel}:x", "sender": sender, "text": text},
    )


def make_service(
    engine: AsyncEngine, bus: EventBus, judge_provider: FakeProvider | None = None
) -> tuple[MonitorService, WakeSink]:
    wake = WakeSink()
    judge = ModelJudge(judge_provider or FakeProvider([]), "judge-model")
    service = MonitorService(make_session_factory(engine), bus, wake, judge)
    return service, wake


async def test_code_predicate_fires_and_wakes_its_thread(
    engine: AsyncEngine,
) -> None:
    bus = EventBus()
    service, wake = make_service(engine, bus)
    monitor_id = await service.create(
        description="watch for urgent",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate=CODE_PREDICATE,
    )
    await bus.publish(inbound("nothing to see"))
    assert wake.messages == []
    await bus.publish(inbound("this is URGENT stuff"))
    assert len(wake.messages) == 1
    woken = wake.messages[0]
    assert woken.thread_key == "cli:home"
    assert woken.sender == "system"
    assert f"monitor #{monitor_id}" in woken.text
    assert "URGENT" in woken.text


async def test_owner_events_are_skipped_strangers_still_fire(
    engine: AsyncEngine,
) -> None:
    """Owner messages already dispatch a turn on every channel, so a monitor
    firing on them would double-wake the agent — the service skips them. A
    stranger's matching message (published, never dispatched) still fires."""
    bus = EventBus()
    service, wake = make_service(engine, bus)
    await service.create(
        description="watch for urgent",
        watch_channel="imessage",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate=CODE_PREDICATE,
    )
    await bus.publish(inbound("urgent!", channel="imessage", sender="owner"))
    assert wake.messages == []
    await bus.publish(inbound("urgent!", channel="imessage"))
    assert len(wake.messages) == 1


async def test_monitor_ignores_other_channels(engine: AsyncEngine) -> None:
    bus = EventBus()
    service, wake = make_service(engine, bus)
    await service.create(
        description="watch cli",
        watch_channel="imessage",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate=CODE_PREDICATE,
    )
    await bus.publish(inbound("urgent", channel="cli"))
    assert wake.messages == []


async def test_model_predicate_asks_the_judge(engine: AsyncEngine) -> None:
    bus = EventBus()
    judge_provider = FakeProvider([text_turn("NO"), text_turn("YES")])
    service, wake = make_service(engine, bus, judge_provider)
    await service.create(
        description="anything about invoices",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate={"kind": "model", "instruction": "Is this about an invoice?"},
    )
    await bus.publish(inbound("hello"))
    assert wake.messages == []
    await bus.publish(inbound("invoice #42 overdue"))
    assert len(wake.messages) == 1
    # The judge saw the instruction and the event payload.
    judged = judge_provider.calls[0][1]["content"]
    assert "Is this about an invoice?" in judged


async def test_deleted_monitor_stops_firing(engine: AsyncEngine) -> None:
    bus = EventBus()
    service, wake = make_service(engine, bus)
    monitor_id = await service.create(
        description="watch",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate=CODE_PREDICATE,
    )
    assert await service.delete(monitor_id) is True
    assert await service.delete(monitor_id) is False
    await bus.publish(inbound("urgent"))
    assert wake.messages == []


async def test_monitor_tools_create_list_delete(engine: AsyncEngine) -> None:
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")

    created = await registry.dispatch(
        ToolCall(
            id="1",
            name="monitor",
            arguments={
                "action": "create",
                "description": "urgent watcher",
                "pattern": "urgent",
            },
        ),
        context,
    )
    assert created == "monitor #1 created"
    listing = await registry.dispatch(
        ToolCall(id="2", name="monitor", arguments={"action": "list"})
    )
    assert "urgent watcher" in listing
    assert "cli:home" in listing
    both = await registry.dispatch(
        ToolCall(
            id="3",
            name="monitor",
            arguments={
                "action": "create",
                "description": "bad",
                "pattern": "x",
                "instruction": "y",
            },
        ),
        context,
    )
    assert both.startswith("error: give exactly one")
    deleted = await registry.dispatch(
        ToolCall(
            id="4", name="monitor", arguments={"action": "delete", "monitor_id": 1}
        )
    )
    assert deleted == "monitor #1 deleted"
    assert await registry.dispatch(
        ToolCall(id="5", name="monitor", arguments={"action": "list"})
    ) == "no monitors"
