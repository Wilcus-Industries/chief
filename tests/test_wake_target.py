"""`MessageStore.resolve_wake_target`: which session a monitor/schedule wakes."""

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.persistence.store import MessageStore


async def test_no_target_uses_the_callers_own_thread(
    engine: AsyncEngine, store: MessageStore
) -> None:
    channel, thread, created = await store.resolve_wake_target(
        None, "cli", "cli:home"
    )
    assert (channel, thread, created) == ("cli", "cli:home", False)


async def test_known_target_keeps_its_own_channel(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("imessage:+1", "imessage")
    channel, thread, created = await store.resolve_wake_target(
        "imessage:+1", "cli", "cli:home"
    )
    # The target's stored channel wins over the caller's — not "cli".
    assert (channel, thread, created) == ("imessage", "imessage:+1", False)


async def test_unknown_target_registers_a_session_under_the_callers_channel(
    engine: AsyncEngine, store: MessageStore
) -> None:
    channel, thread, created = await store.resolve_wake_target(
        "web:errands", "cli", "cli:home"
    )
    assert (channel, thread, created) == ("cli", "web:errands", True)
    # The row now exists so a later wake resolves it as known.
    assert await store.channel("web:errands") == "cli"
    again = await store.resolve_wake_target("web:errands", "cli", "cli:home")
    assert again == ("cli", "web:errands", False)
