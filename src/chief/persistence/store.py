"""Message store: persist and reload per-thread transcripts."""

from typing import Any

from sqlalchemy import delete, func, select

from chief.persistence.db import SessionFactory
from chief.persistence.models import MessageRow, SessionRow


class MessageStore:
    """Reads and writes session transcripts, one commit per operation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    async def ensure_session(
        self,
        thread_key: str,
        channel: str,
        stream_policy: dict[str, Any] | None = None,
    ) -> None:
        """Create the session row if this thread has never been seen.

        ``stream_policy`` seeds the per-thread override at creation only — an
        existing row keeps whatever policy it already has."""
        async with self._factory() as db:
            if await db.get(SessionRow, thread_key) is None:
                db.add(
                    SessionRow(
                        thread_key=thread_key,
                        channel=channel,
                        stream_policy=stream_policy,
                    )
                )
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

    async def channel(self, thread_key: str) -> str | None:
        """The thread's origin channel — the first device that spoke on it."""
        async with self._factory() as db:
            row = await db.get(SessionRow, thread_key)
            return row.channel if row else None

    async def resolve_wake_target(
        self, target: str | None, default_channel: str, default_thread: str
    ) -> tuple[str, str] | None:
        """Pick the (channel, thread) a monitor or schedule should wake.

        No target means the caller's own thread. A named target must already be
        a known session; its own channel is used, so the wake routes to the
        adapter that owns it. Returns ``None`` if the target names no existing
        session — the caller turns that into an error rather than silently
        creating a new (possibly external) recipient.
        """
        if not target:
            return default_channel, default_thread
        channel = await self.channel(target)
        if channel is None:
            return None
        return channel, target

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

    async def stream_policy(self, thread_key: str) -> dict[str, Any] | None:
        """The thread's persisted stream-policy override, if any."""
        async with self._factory() as db:
            row = await db.get(SessionRow, thread_key)
            return row.stream_policy if row else None

    async def set_stream_policy(
        self, thread_key: str, policy: dict[str, Any] | None
    ) -> None:
        """Persist (or clear) the thread's stream-policy override."""
        async with self._factory() as db:
            row = await db.get(SessionRow, thread_key)
            if row is not None:
                row.stream_policy = policy
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

    async def clear(self, thread_key: str) -> None:
        """Wipe a thread's transcript but keep its session row (``/clear``)."""
        await self.replace(thread_key, [])

    async def delete_session(self, thread_key: str) -> None:
        """Delete a thread's session row and its whole transcript (prune)."""
        async with self._factory() as db:
            await db.execute(
                delete(MessageRow).where(MessageRow.thread_key == thread_key)
            )
            await db.execute(
                delete(SessionRow).where(SessionRow.thread_key == thread_key)
            )
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
