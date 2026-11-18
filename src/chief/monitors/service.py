"""Monitor service: persists monitors, watches the bus, wakes the agent.

Predicates come in two kinds: ``code`` (a cheap regex over one event payload
field) and ``classifier`` (a named categorical-label classifier, which must be
scoped — see predicate.py). The tool's three forms are ``pattern`` (code),
``instruction`` (sugar for the wake-judge classifier), and ``classifier``.
Routing an event to its wakes lives in router.py, matching and the injection
screen in gate.py, and what a judge reads and a wake carries (the thread's
chat log) in wake.py.
"""

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from chief.bus import EventBus
from chief.classifiers import Classifier, ClassifierDef
from chief.contacts import Roster
from chief.monitors.gate import MonitorGate
from chief.monitors.history import HistorySource, SeenCursors
from chief.monitors.predicate import is_unscoped_classifier
from chief.monitors.router import MonitorRouter, WakeAgent
from chief.monitors.transcript import Namer
from chief.monitors.wake import WakeLog
from chief.persistence.db import SessionFactory
from chief.persistence.models import MonitorRow
from chief.persistence.store import MessageStore
from chief.screen import Screen

logger = logging.getLogger(__name__)

__all__ = ["MonitorService", "WakeAgent"]


class MonitorService:
    """Loads monitors, subscribes to the bus, and fires wakes."""

    def __init__(
        self,
        factory: SessionFactory,
        bus: EventBus,
        wake: WakeAgent,
        classifier: Classifier,
        screen: Screen | None = None,
        owner_handles: tuple[str, ...] = (),
        roster: Roster | None = None,
        histories: Mapping[str, HistorySource] | None = None,
        cursors: SeenCursors | None = None,
    ) -> None:
        self._factory = factory
        self.store = MessageStore(factory)  # the tool resolves wake targets here
        self._classifier = classifier
        self._cursors = cursors or SeenCursors()
        log = WakeLog(histories or {}, Namer(roster, owner_handles), self._cursors)
        router = MonitorRouter(
            self.list_monitors, MonitorGate(classifier, screen), log, wake
        )
        bus.subscribe(router.on_event)

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

    async def list_monitors(self, include_disabled: bool = False) -> list[MonitorRow]:
        query = select(MonitorRow).order_by(MonitorRow.id)
        if not include_disabled:
            query = query.where(MonitorRow.enabled)
        async with self._factory() as db:
            return list(await db.scalars(query))

    async def disable_unscoped(self) -> list[tuple[int, str]]:
        """Disable unscoped classifier monitors at boot, naming each (#285).

        Fail closed: these fed every stranger's message to a model. The warning
        names them so the owner rescopes deliberately, not by noticing silence.
        """
        disabled = []
        async with self._factory() as db:
            for row in await db.scalars(select(MonitorRow).where(MonitorRow.enabled)):
                if not is_unscoped_classifier(row.predicate):
                    continue
                row.enabled = False
                disabled.append((row.id, row.description))
                logger.warning(
                    "monitor %s DISABLED, unscoped classifier (#285): %s — "
                    "recreate it with scope_sender or scope_thread",
                    row.id, row.description,
                )
            await db.commit()
        return disabled

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

    def forget_wake(self, wake_thread: str) -> None:
        """That session was wiped: its monitors' next wakes bring context again."""
        self._cursors.forget_wake(wake_thread)

    def classifier_def(self, name: str) -> ClassifierDef | None:
        return self._classifier.definition(name)
