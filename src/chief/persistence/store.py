"""Message store: persist and reload per-thread transcripts."""

from typing import Any

from sqlalchemy import select

from chief.persistence.db import SessionFactory
from chief.persistence.models import MessageRow, SessionRow


class MessageStore:
    """Reads and writes session transcripts, one commit per operation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    async def ensure_session(self, thread_key: str, channel: str) -> None:
        """Create the session row if this thread has never been seen."""
        async with self._factory() as db:
            if await db.get(SessionRow, thread_key) is None:
                db.add(SessionRow(thread_key=thread_key, channel=channel))
                await db.commit()

    async def append(self, thread_key: str, messages: list[dict[str, Any]]) -> None:
        """Persist new transcript entries for a thread."""
        async with self._factory() as db:
            for message in messages:
                db.add(MessageRow(thread_key=thread_key, message=message))
            await db.commit()

    async def load(self, thread_key: str) -> list[dict[str, Any]]:
        """Reload a thread's transcript in insertion order (restart resume)."""
        async with self._factory() as db:
            rows = await db.scalars(
                select(MessageRow)
                .where(MessageRow.thread_key == thread_key)
                .order_by(MessageRow.id)
            )
            return [dict(row.message) for row in rows]
