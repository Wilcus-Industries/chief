"""HookRegistry: package-tagged agent-loop hook registrations.

Three kinds, each an async callable a package registers under its name:

- ``pre_turn`` ``() -> str | None`` — context added to every turn
- ``session_start`` ``() -> str | None`` — context added on a thread's first turn
- ``post_turn`` ``(result, messages) -> None`` — observes a finished turn

Read accessors return entries sorted by package name (stable), so a package's
placement in the assembled prompt is deterministic regardless of the order it
registered or was discovered in.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from chief.agent.loop import TurnResult

PreTurnHook = Callable[[], Awaitable[str | None]]
SessionStartHook = Callable[[], Awaitable[str | None]]
PostTurnHook = Callable[[TurnResult, list[dict[str, Any]]], Awaitable[None]]


class HookRegistry:
    """Central store of every installed package's hook registrations."""

    def __init__(self) -> None:
        self._pre_turn: list[tuple[str, PreTurnHook]] = []
        self._session_start: list[tuple[str, SessionStartHook]] = []
        self._post_turn: list[tuple[str, PostTurnHook]] = []

    def register_pre_turn(self, package: str, fn: PreTurnHook) -> None:
        self._pre_turn.append((package, fn))

    def register_session_start(self, package: str, fn: SessionStartHook) -> None:
        self._session_start.append((package, fn))

    def register_post_turn(self, package: str, fn: PostTurnHook) -> None:
        self._post_turn.append((package, fn))

    def pre_turn(self) -> list[tuple[str, PreTurnHook]]:
        return sorted(self._pre_turn, key=lambda entry: entry[0])

    def session_start(self) -> list[tuple[str, SessionStartHook]]:
        return sorted(self._session_start, key=lambda entry: entry[0])

    def post_turn(self) -> list[tuple[str, PostTurnHook]]:
        return sorted(self._post_turn, key=lambda entry: entry[0])


class PackageHookRegistrar:
    """Per-package facade handed to a package's ``register(context, hooks)``.

    Its decorator-style methods tag every registration with the package name,
    so the registry can attribute and order contributions. Each returns the
    function it was given, so ``@hooks.pre_turn`` reads as a decorator.
    """

    def __init__(self, registry: HookRegistry, package: str) -> None:
        self._registry = registry
        self._package = package

    def pre_turn(self, fn: PreTurnHook) -> PreTurnHook:
        self._registry.register_pre_turn(self._package, fn)
        return fn

    def session_start(self, fn: SessionStartHook) -> SessionStartHook:
        self._registry.register_session_start(self._package, fn)
        return fn

    def post_turn(self, fn: PostTurnHook) -> PostTurnHook:
        self._registry.register_post_turn(self._package, fn)
        return fn
