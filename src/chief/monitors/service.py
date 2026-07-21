"""Monitor service: persists monitors, watches the bus, wakes the agent.

Predicates come in two kinds: ``code`` (a cheap regex over an event payload
field) and ``classifier`` (a named categorical-label classifier that fires on
one label). The agent-facing tool exposes three forms — ``pattern`` (code),
``instruction`` (sugar lowering to the built-in wake-judge classifier), and a
named ``classifier`` + ``fire_label``.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from chief.adapters.base import Message
from chief.bus import Event, EventBus
from chief.classifiers import Classifier, ClassifierDef
from chief.persistence.db import SessionFactory
from chief.persistence.models import MonitorRow
from chief.persistence.store import MessageStore

logger = logging.getLogger(__name__)

WakeAgent = Callable[[Message], Awaitable[None]]


class MonitorService:
    """Loads monitors, subscribes to the bus, and fires wakes."""

    def __init__(
        self,
        factory: SessionFactory,
        bus: EventBus,
        wake: WakeAgent,
        classifier: Classifier,
    ) -> None:
        self._factory = factory
        self.store = MessageStore(factory)  # the tool resolves wake targets here
        self._wake = wake
        self._classifier = classifier
        bus.subscribe(self._on_event)

    async def create(
        self,
        *,
        description: str,
        watch_channel: str,
        wake_channel: str,
        wake_thread: str,
        predicate: dict[str, Any],
    ) -> int:
        async with self._factory() as db:
            row = MonitorRow(
                description=description,
                watch_channel=watch_channel,
                wake_channel=wake_channel,
                wake_thread=wake_thread,
                predicate=predicate,
            )
            db.add(row)
            await db.commit()
            return row.id

    async def list_enabled(self) -> list[MonitorRow]:
        async with self._factory() as db:
            rows = await db.scalars(
                select(MonitorRow).where(MonitorRow.enabled).order_by(MonitorRow.id)
            )
            return list(rows)

    async def retarget(
        self, monitor_id: int, target: str | None,
        default_channel: str, default_thread: str,
    ) -> tuple[str, str]:
        """Re-point which session a monitor wakes.

        Returns ``(status, wake_thread)``: status is ``"ok"``, ``"missing"``,
        or ``"unknown-target"``. The target is resolved only after the row is
        found, so a rejected retarget writes nothing.
        """
        async with self._factory() as db:
            row = await db.get(MonitorRow, monitor_id)
            if row is None:
                return "missing", ""
            resolved = await self.store.resolve_wake_target(
                target, default_channel, default_thread
            )
            if resolved is None:
                return "unknown-target", ""
            row.wake_channel, row.wake_thread = resolved
            await db.commit()
            return "ok", resolved[1]

    async def delete(self, monitor_id: int) -> bool:
        async with self._factory() as db:
            row = await db.get(MonitorRow, monitor_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    def classifier_def(self, name: str) -> ClassifierDef | None:
        return self._classifier.definition(name)

    async def _on_event(self, event: Event) -> None:
        # Owner messages already dispatch a turn on every channel, so a monitor
        # firing on them would double-wake the agent. Strangers (published but
        # never dispatched) are the intended trigger.
        if event.payload.get("sender") == "owner":
            return
        for monitor in await self.list_enabled():
            if monitor.watch_channel != event.channel:
                continue
            # One monitor's classifier raising must not abort the sibling loop.
            try:
                if await self._matches(monitor, event):
                    await self._fire(monitor, event)
            except Exception:
                logger.exception("monitor %s errored", monitor.id)

    async def _matches(self, monitor: MonitorRow, event: Event) -> bool:
        predicate: dict[str, Any] = dict(monitor.predicate)
        if predicate.get("kind") == "code":
            field = str(predicate.get("field", "text"))
            value = str(event.payload.get(field, ""))
            return re.search(str(predicate["pattern"]), value, re.I) is not None
        if predicate.get("kind") == "classifier":
            text = json.dumps(event.payload)
            instruction = predicate.get("instruction")
            if instruction is not None:
                text = f"{instruction}\n\nEvent:\n{text}"
            label = await self._classifier.classify(
                str(predicate["classifier"]), text
            )
            return label == str(predicate["fire_label"])
        logger.warning("monitor %s has unknown predicate kind", monitor.id)
        return False

    async def _fire(self, monitor: MonitorRow, event: Event) -> None:
        logger.info("monitor %s fired on %s", monitor.id, event.type)
        text = (
            f"[monitor #{monitor.id} fired: {monitor.description}]\n"
            f"event: {json.dumps(event.payload)}"
        )
        # sender="system": runs a turn but is never re-published to the bus,
        # so a monitor can't trigger itself.
        await self._wake(
            Message(
                channel=monitor.wake_channel,
                sender="system",
                thread_key=monitor.wake_thread,
                text=text,
            )
        )
