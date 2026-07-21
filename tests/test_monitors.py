"""Monitors: predicates, wakes, persistence, and the agent-facing tools."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.agent.tools import ToolContext, ToolRegistry
from chief.bus import Event, EventBus
from chief.classifiers import Classifier, ClassifierRegistry
from chief.monitors.service import MonitorService
from chief.monitors.tools import register_monitor_tools
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.provider.base import ToolCall

from .fakes import FakeProvider, text_turn

REPO = Path(__file__).parent.parent

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
    classifier = Classifier(
        judge_provider or FakeProvider([]),
        ClassifierRegistry(REPO / "classifiers"),
        "judge-model",
    )
    service = MonitorService(make_session_factory(engine), bus, wake, classifier)
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


async def test_instruction_predicate_asks_the_judge(engine: AsyncEngine) -> None:
    """The instruction form lowers to a wake-judge classifier predicate; the
    instruction text still reaches the prompt and it fires on YES."""
    bus = EventBus()
    judge_provider = FakeProvider([text_turn("NO"), text_turn("YES")])
    service, wake = make_service(engine, bus, judge_provider)
    await service.create(
        description="anything about invoices",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate={
            "kind": "classifier",
            "classifier": "wake-judge",
            "fire_label": "YES",
            "instruction": "Is this about an invoice?",
        },
    )
    await bus.publish(inbound("hello"))
    assert wake.messages == []
    await bus.publish(inbound("invoice #42 overdue"))
    assert len(wake.messages) == 1
    # The judge saw the instruction and the event payload.
    judged = judge_provider.calls[0][1]["content"]
    assert "Is this about an invoice?" in judged


async def test_classifier_predicate_fires_on_its_label(engine: AsyncEngine) -> None:
    bus = EventBus()
    judge_provider = FakeProvider([text_turn("NO"), text_turn("YES")])
    service, wake = make_service(engine, bus, judge_provider)
    await service.create(
        description="wake when the judge says yes",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate={
            "kind": "classifier",
            "classifier": "wake-judge",
            "fire_label": "YES",
        },
    )
    await bus.publish(inbound("nope"))
    assert wake.messages == []
    await bus.publish(inbound("yep"))
    assert len(wake.messages) == 1


async def test_classifier_error_does_not_block_siblings(
    engine: AsyncEngine,
) -> None:
    """A monitor whose classifier raises must not abort the sibling loop."""
    bus = EventBus()
    service, wake = make_service(engine, bus)
    # Lower id, evaluated first: an unknown classifier name raises.
    await service.create(
        description="broken",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate={"kind": "classifier", "classifier": "ghost", "fire_label": "YES"},
    )
    await service.create(
        description="urgent watcher",
        watch_channel="cli",
        wake_channel="cli",
        wake_thread="cli:home",
        predicate=CODE_PREDICATE,
    )
    await bus.publish(inbound("this is URGENT"))
    assert len(wake.messages) == 1


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


async def test_monitor_tool_create_targets_another_session(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """`target_session` routes the wake to a different, known session, which
    keeps its own channel."""
    await store.ensure_session("web:errands", "web")
    bus = EventBus()
    service, wake = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")

    created = await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "create", "description": "urgent watcher",
            "pattern": "urgent", "target_session": "web:errands"}),
        context,
    )
    assert created == "monitor #1 created"
    await bus.publish(inbound("this is URGENT"))
    assert len(wake.messages) == 1
    assert wake.messages[0].thread_key == "web:errands"
    assert wake.messages[0].channel == "web"


async def test_monitor_tool_create_registers_an_unknown_target(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """An unknown `target_session` is registered under the caller's channel and
    the tool says so."""
    bus = EventBus()
    service, wake = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")

    created = await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "create", "description": "urgent watcher",
            "pattern": "urgent", "target_session": "web:new"}),
        context,
    )
    assert created == "monitor #1 created (registered new session 'web:new')"
    assert await store.channel("web:new") == "cli"
    await bus.publish(inbound("this is URGENT"))
    assert wake.messages[0].thread_key == "web:new"
    assert wake.messages[0].channel == "cli"


async def test_monitor_tool_retarget_repoints_the_wake(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("web:errands", "web")
    bus = EventBus()
    service, wake = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")

    await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "create", "description": "urgent watcher",
            "pattern": "urgent"}),
        context,
    )
    retargeted = await registry.dispatch(
        ToolCall(id="2", name="monitor", arguments={
            "action": "retarget", "monitor_id": 1,
            "target_session": "web:errands"}),
        context,
    )
    assert retargeted == "monitor #1 now wakes web:errands"
    listing = await registry.dispatch(
        ToolCall(id="3", name="monitor", arguments={"action": "list"})
    )
    assert "web:errands" in listing
    await bus.publish(inbound("this is URGENT"))
    assert wake.messages[0].thread_key == "web:errands"
    assert wake.messages[0].channel == "web"


async def test_monitor_tool_retarget_unknown_id(engine: AsyncEngine) -> None:
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")
    result = await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "retarget", "monitor_id": 999, "target_session": "x:y"}),
        context,
    )
    assert result == "error: no such monitor"


async def test_monitor_tool_classifier_form(engine: AsyncEngine) -> None:
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
                "description": "wake on yes",
                "classifier": "wake-judge",
                "fire_label": "YES",
            },
        ),
        context,
    )
    assert created == "monitor #1 created"
    no_label = await registry.dispatch(
        ToolCall(
            id="2",
            name="monitor",
            arguments={
                "action": "create",
                "description": "missing label",
                "classifier": "wake-judge",
            },
        ),
        context,
    )
    assert "fire_label" in no_label


async def test_monitor_tool_rejects_unknown_classifier(engine: AsyncEngine) -> None:
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")
    result = await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "create", "description": "x",
            "classifier": "ghost", "fire_label": "YES"}),
        context,
    )
    assert "unknown classifier" in result


async def test_monitor_tool_rejects_undeclared_fire_label(
    engine: AsyncEngine,
) -> None:
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="cli")
    result = await registry.dispatch(
        ToolCall(id="1", name="monitor", arguments={
            "action": "create", "description": "x",
            "classifier": "wake-judge", "fire_label": "yes"}),
        context,
    )
    assert "fire_label" in result and "wake-judge" in result


async def test_pattern_monitor_can_match_on_sender(engine: AsyncEngine) -> None:
    """#235: a pattern monitor could only ever see `text`.

    Asking to wake on a specific contact silently never fired, because the
    event's `text` is just the message body — the sender is a separate field.
    """
    bus = EventBus()
    service, wake = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="imessage")

    created = await registry.dispatch(
        ToolCall(
            id="1",
            name="monitor",
            arguments={
                "action": "create",
                "description": "wake on Daniel",
                "pattern": r"\+15551234567",
                "field": "sender",
            },
        ),
        context,
    )
    assert created == "monitor #1 created"

    await bus.publish(
        Event(
            type="message.inbound",
            channel="imessage",
            payload={
                "thread_key": "imessage:+15551234567",
                "sender": "+15551234567",
                "text": "Chastain?",  # body alone would never match the pattern
            },
        )
    )
    assert len(wake.messages) == 1, "sender-matched monitor must fire"


async def test_pattern_monitor_rejects_an_unmatchable_field(
    engine: AsyncEngine,
) -> None:
    """A typo'd field would match "" forever and never fire — fail loudly."""
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="imessage")

    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="monitor",
            arguments={
                "action": "create",
                "description": "typo",
                "pattern": "x",
                "field": "from",
            },
        ),
        context,
    )
    assert result.startswith("error:")
    assert "from" in result
    assert "sender" in result, "the error must list the fields that do work"


async def test_field_without_pattern_is_rejected(engine: AsyncEngine) -> None:
    """`field` only means anything for the pattern form."""
    bus = EventBus()
    service, _ = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="imessage")

    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="monitor",
            arguments={
                "action": "create",
                "description": "x",
                "instruction": "wake on anything",
                "field": "sender",
            },
        ),
        context,
    )
    assert result.startswith("error:")
    assert "field" in result and "pattern" in result


async def test_pattern_defaults_to_text_field(engine: AsyncEngine) -> None:
    """Backward compatible: omitting `field` keeps matching on text."""
    bus = EventBus()
    service, wake = make_service(engine, bus)
    registry = ToolRegistry()
    register_monitor_tools(registry, service)
    context = ToolContext(thread_key="cli:home", channel="imessage")

    await registry.dispatch(
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
    await bus.publish(
        Event(
            type="message.inbound",
            channel="imessage",
            payload={"thread_key": "t", "sender": "+1", "text": "urgent thing"},
        )
    )
    assert len(wake.messages) == 1
