"""Audit log JSONL append; event bus delivery and failure isolation."""

import json
from pathlib import Path

from chief.audit import AuditLog
from chief.bus import Event, EventBus


def test_audit_appends_json_lines(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("tool_call", tool="echo", outcome="list:approved")
    log.record("gate", decision="never")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "tool_call"
    assert first["tool"] == "echo"
    assert "ts" in first


async def test_bus_delivers_to_all_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def a(event: Event) -> None:
        seen.append(f"a:{event.type}")

    async def b(event: Event) -> None:
        seen.append(f"b:{event.type}")

    bus.subscribe(a)
    unsubscribe_b = bus.subscribe(b)
    await bus.publish(Event(type="x", channel="cli", payload={}))
    unsubscribe_b()
    await bus.publish(Event(type="y", channel="cli", payload={}))
    assert seen == ["a:x", "b:x", "a:y"]


async def test_bus_isolates_a_failing_handler() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def bad(event: Event) -> None:
        raise RuntimeError("boom")

    async def good(event: Event) -> None:
        seen.append(event.type)

    bus.subscribe(bad)
    bus.subscribe(good)
    await bus.publish(Event(type="x", channel="cli", payload={}))
    assert seen == ["x"]
