"""The #133 broadcast bus: mirror every stack's engine outbound onto the socket.

:class:`MirrorTaskIO` wraps a platform IO (CLI / Telegram / Discord) at the ``TaskIO``
seam. On ``send``/``send_file`` it delivers to the real platform IO **first**, then
broadcasts a platform-tagged frame onto the client-plane socket
(:mod:`chief.client_plane`) and records the message in the ``message_log`` table. The
mirror tail is fully contained: a broadcast or DB failure is logged, never raised, so
phone-side delivery is never disturbed. If the inner IO itself raises, mirroring is
skipped and the exception propagates exactly as today.

Only ``send`` and ``send_file`` are mirrored/logged — the engine's task milestones and
replies. ``create_thread``/``archive_thread`` and the three card methods are pure
delegation: approval cards on the socket are #136's slice, budget traffic stays
unmirrored.

``server=None`` is passed for the CLI stack, whose inner ``CliTaskIO`` already
broadcasts (the socket *is* its delivery); there the mirror adds only the message-log
record, never a duplicate frame.
"""

import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..client_plane import SocketServer, file_frame, outbound_frame
from ..gate.approvals import ApprovalCard
from ..persistence import message_log
from .base import BudgetCard

logger = logging.getLogger(__name__)


class PlatformIO(Protocol):
    """The full platform IO surface the mirror wraps (TaskIO + ApprovalIO + BudgetIO).

    Declared structurally — not by importing ``TaskIO``/``ApprovalIO``/``BudgetIO`` from
    ``core``/``gate`` — so the adapters layer keeps its no-import-of-core.tasks
    direction (mirrors how ``cli.py`` imports only ``ApprovalCard`` and ``BudgetCard``).
    These are the exact seven methods the platform IOs implement.
    """

    async def send(self, thread_key: str, text: str) -> None: ...
    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None: ...
    async def create_thread(self, *, like_thread_key: str, title: str) -> str: ...
    async def archive_thread(self, thread_key: str) -> None: ...
    async def send_card(self, route: str, card: ApprovalCard) -> str: ...
    async def edit_card(self, msg_ref: str, text: str) -> None: ...
    async def send_budget_card(self, route: str, card: BudgetCard) -> None: ...


class MirrorTaskIO:
    """Wrap a platform IO: deliver first, then broadcast + log the outbound (#133)."""

    def __init__(
        self,
        inner: PlatformIO,
        *,
        platform: str,
        session_factory: async_sessionmaker[AsyncSession],
        server: SocketServer | None,
    ) -> None:
        self._inner = inner
        self._platform = platform
        self._session_factory = session_factory
        #: ``None`` for the CLI stack, whose inner IO already broadcasts — the mirror
        #: then adds only the message-log record, never a second frame.
        self._server = server

    async def send(self, thread_key: str, text: str) -> None:
        """Deliver to the platform, then broadcast + log the milestone/reply (#133)."""
        await self._inner.send(thread_key, text)
        frame = outbound_frame(thread_key, text, platform=self._platform)
        await self._mirror(
            frame,
            kind=str(frame["type"]),
            text=str(frame["text"]),
            filename=None,
        )

    async def send_file(
        self,
        thread_key: str,
        filename: str,
        data: bytes,
        caption: str | None = None,
    ) -> None:
        """Deliver the file, then broadcast + log it (no bytes in the row, #133)."""
        await self._inner.send_file(thread_key, filename, data, caption)
        frame = file_frame(
            thread_key, filename, data, caption, platform=self._platform
        )
        await self._mirror(
            frame,
            kind=message_log.KIND_FILE,
            text=caption or "",
            filename=filename,
        )

    async def _mirror(
        self,
        frame: dict[str, object],
        *,
        kind: str,
        text: str,
        filename: str | None,
    ) -> None:
        """Broadcast ``frame`` (when a server is set) and record the row (#133).

        Wrapped in a blanket ``except`` on purpose: mirroring is a best-effort side
        channel, so a broadcast or DB failure must never break — or reorder — the
        platform delivery that already happened before this call.
        """
        try:
            if self._server is not None:
                await self._server.broadcast(frame)
            async with self._session_factory() as session:
                await message_log.record(
                    session,
                    platform=self._platform,
                    thread_key=str(frame["thread_key"]),
                    role=message_log.ROLE_ASSISTANT,
                    kind=kind,
                    text=text,
                    filename=filename,
                )
                await session.commit()
        except Exception:
            logger.exception("mirror failed for %s frame", kind)

    async def create_thread(self, *, like_thread_key: str, title: str) -> str:
        """Delegate — no mirroring (thread lifecycle is not owner-visible traffic)."""
        return await self._inner.create_thread(
            like_thread_key=like_thread_key, title=title
        )

    async def archive_thread(self, thread_key: str) -> None:
        """Delegate — no mirroring."""
        await self._inner.archive_thread(thread_key)

    async def send_card(self, route: str, card: ApprovalCard) -> str:
        """Delegate — cards on the socket are #136's slice (no mirroring)."""
        return await self._inner.send_card(route, card)

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Delegate — cards on the socket are #136's slice (no mirroring)."""
        await self._inner.edit_card(msg_ref, text)

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Delegate — budget traffic stays unmirrored (no mirroring)."""
        await self._inner.send_budget_card(route, card)
