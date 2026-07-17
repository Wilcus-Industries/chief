"""Message store: persist and reload per-thread transcripts."""

from typing import Any

from sqlalchemy import delete, func, select

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

    async def has_sessions(self) -> bool:
        """Whether any thread has ever existed (false = fresh install)."""
        async with self._factory() as db:
            row = await db.scalar(select(SessionRow.thread_key).limit(1))
            return row is not None

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Every thread with its channel, model, message count and last activity.

        Newest activity first — the web cockpit's buffer list. A thread with no
        messages yet falls back to its own creation time so it still sorts.
        """
        async with self._factory() as db:
            rows = await db.execute(
                select(
                    SessionRow.thread_key,
                    SessionRow.channel,
                    SessionRow.model_override,
                    SessionRow.created_at,
                    func.count(MessageRow.id),
                    func.max(MessageRow.created_at),
                )
                .outerjoin(
                    MessageRow, MessageRow.thread_key == SessionRow.thread_key
                )
                .group_by(SessionRow.thread_key)
            )
            sessions = [
                {
                    "thread": thread_key,
                    "channel": channel,
                    "model": model,
                    "count": count,
                    "last": (last or created).isoformat(),
                }
                for thread_key, channel, model, created, count, last in rows
            ]
        sessions.sort(key=lambda item: item["last"], reverse=True)
        return sessions

    async def model_override(self, thread_key: str) -> str | None:
        """The owner's per-thread model override, if any."""
        async with self._factory() as db:
            row = await db.get(SessionRow, thread_key)
            return row.model_override if row else None

    async def set_model_override(self, thread_key: str, model: str) -> None:
        """Persist the owner's per-thread model override."""
        async with self._factory() as db:
            row = await db.get(SessionRow, thread_key)
            if row is not None:
                row.model_override = model
                await db.commit()

    async def append(self, thread_key: str, messages: list[dict[str, Any]]) -> None:
        """Persist new transcript entries for a thread."""
        async with self._factory() as db:
            for message in messages:
                db.add(MessageRow(thread_key=thread_key, message=message))
            await db.commit()

    async def replace(
        self, thread_key: str, messages: list[dict[str, Any]]
    ) -> None:
        """Swap a thread's whole persisted transcript (compaction), one commit."""
        async with self._factory() as db:
            await db.execute(
                delete(MessageRow).where(MessageRow.thread_key == thread_key)
            )
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
