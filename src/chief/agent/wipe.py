"""The one guarded path for wiping a thread (``clear`` / ``delete``).

Split from ``chief.agent.manager`` to keep it under the length cap.
"""

from collections.abc import Awaitable, Callable

from chief.agent.session import Session

StoreWipe = Callable[[str], Awaitable[None]]


async def wipe_thread(
    sessions: dict[str, Session], thread_key: str, op: StoreWipe
) -> bool:
    """Run a store wipe under the session lock — the single guard for every
    caller (``session`` tool, ``/clear``, ``/prune``, web cockpit). Refuses
    (``False``, no write) when a cached session is mid-turn: its in-flight tail
    would commit orphan rows over the wipe. Holding the lock across the wipe
    shuts the delete/clear TOCTOU a plain ``is_busy`` pre-check leaves open.
    The cached session is dropped on success — a DB wipe alone is cosmetic
    while a loaded ``Session`` still holds the old history."""
    session = sessions.get(thread_key)
    if session is None:
        await op(thread_key)
        return True
    if session.busy:  # sync check; the lock grab below runs before any await
        return False
    async with session.lock:
        await op(thread_key)
    sessions.pop(thread_key, None)
    return True
