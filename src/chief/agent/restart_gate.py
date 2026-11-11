"""The restart gate seam: bracket a turn so a pending self-edit restart drains
it before exec.

Kept apart from the session so the turn path stays focused on assembling and
running turns. ``chief.selfedit.recovery.RestartController`` is the production
implementation; ``_NullGate`` is the no-op used by tests and non-daemon runs.
"""

from typing import Protocol


class TurnTicket(Protocol):
    """One turn's entry in the durable in-flight record
    (``chief.selfedit.inflight``): ``started`` once its user message is in
    the transcript, ``done`` when it finishes. A turn killed before ``done``
    is replayed or woken by the next boot."""

    def started(self) -> None: ...

    def done(self) -> None: ...


class RestartGate(Protocol):
    """Brackets a turn so a pending self-edit restart drains it before exec.

    ``enter_turn`` blocks while a restart is pending and otherwise registers
    the turn as active; ``leave_turn`` deregisters it; ``fire_if_requested``
    restarts (never returning) once the turn has committed. ``track`` records
    a turn the moment it is queued, so a restart that kills it can't lose it.
    """

    def track(
        self, thread_key: str, channel: str, sender: str, text: str
    ) -> TurnTicket: ...

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...


class NullTicket:
    def started(self) -> None: ...

    def done(self) -> None: ...


class _NullGate:
    """No-op gate for sessions with no restart coordinator (tests, non-daemon)."""

    def track(
        self, thread_key: str, channel: str, sender: str, text: str
    ) -> TurnTicket:
        return NullTicket()

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...
