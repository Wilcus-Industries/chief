"""The restart gate seam: bracket a turn so a pending self-edit restart drains
it before exec.

Kept apart from the session so the turn path stays focused on assembling and
running turns. ``chief.selfedit.restart.RestartController`` is the production
implementation; ``_NullGate`` is the no-op used by tests and non-daemon runs.
"""

from typing import Protocol


class RestartGate(Protocol):
    """Brackets a turn so a pending self-edit restart drains it before exec.

    ``enter_turn`` blocks while a restart is pending and otherwise registers
    the turn as active; ``leave_turn`` deregisters it; ``fire_if_requested``
    restarts (never returning) once the turn has committed.
    """

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...


class _NullGate:
    """No-op gate for sessions with no restart coordinator (tests, non-daemon)."""

    async def enter_turn(self) -> None: ...

    def leave_turn(self) -> None: ...

    async def fire_if_requested(self) -> None: ...
