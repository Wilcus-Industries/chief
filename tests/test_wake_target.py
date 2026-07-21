"""`MessageStore.resolve_wake_target`: which session a monitor/schedule wakes."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.persistence.store import MessageStore


async def test_no_target_uses_the_callers_own_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    resolved = await store.resolve_wake_target(None, "cli", "cli:home")
    assert resolved == ("cli", "cli:home")


async def test_known_target_keeps_its_own_channel(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("imessage:+1", "imessage")
    resolved = await store.resolve_wake_target("imessage:+1", "cli", "cli:home")
    # The target's stored channel wins over the caller's — routes to imessage.
    assert resolved == ("imessage", "imessage:+1")


async def test_unknown_target_is_rejected_and_creates_nothing(
    engine: AsyncEngine, store: MessageStore
) -> None:
    resolved = await store.resolve_wake_target("web:ghost", "cli", "cli:home")
    assert resolved is None
    # No session was silently registered for the unknown target.
    assert await store.channel("web:ghost") is None
