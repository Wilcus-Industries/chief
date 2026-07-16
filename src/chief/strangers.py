"""Stranger log: metadata-only records of unknown senders (no content kept)."""

import logging

from sqlalchemy import select

from chief.adapters.base import Message
from chief.persistence.db import SessionFactory
from chief.persistence.models import StrangerRow

logger = logging.getLogger(__name__)


class StrangerLog:
    """Records who tried to reach the agent; never what they said."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    async def log(self, message: Message) -> None:
        logger.info(
            "stranger message on %s from %s (logged, not answered)",
            message.channel,
            message.sender,
        )
        async with self._factory() as db:
            db.add(
                StrangerRow(
                    channel=message.channel,
                    sender=message.sender,
                    thread_key=message.thread_key,
                )
            )
            await db.commit()

    async def list_recent(self, limit: int = 50) -> list[StrangerRow]:
        async with self._factory() as db:
            rows = await db.scalars(
                select(StrangerRow).order_by(StrangerRow.id.desc()).limit(limit)
            )
            return list(rows)
