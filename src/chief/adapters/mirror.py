"""The #133 broadcast bus: mirror the chat stacks' engine outbound onto the socket.

:class:`MirrorTaskIO` wraps a **chat** platform IO (Telegram / Discord) at the
``TaskIO`` seam. On ``send``/``send_file`` it delivers to the platform IO **first**,
then broadcasts a platform-tagged frame onto the client-plane socket
(:mod:`chief.client_plane`) and records the message in the ``message_log`` table through
the shared :class:`~chief.persistence.messages.MessageLog` — the one recorder over that
table. The mirror tail is fully contained: a broadcast or DB failure is logged, never
raised, so phone-side delivery is never disturbed. If the inner IO itself raises,
mirroring is skipped and the exception propagates exactly as today.

``send``, ``send_file``, and the approval card methods are mirrored/logged. Cards are
now mirrored too (#136): ``send_card``/``edit_card`` post to the platform first, then
broadcast a platform-tagged ``card``/``card_resolved`` frame. ``create_thread`` /
``archive_thread`` / ``send_budget_card`` stay pure delegation: thread lifecycle isn't
owner-visible traffic and budget traffic stays unmirrored.

The CLI stack is **not** wrapped: its :class:`~chief.adapters.cli.CliTaskIO` already
broadcasts (the socket *is* its delivery) and already records each outbound — with the
payload and the pre-broadcast ``delivered`` snapshot that #132 detach-replay needs.
Wrapping it would log every CLI message twice. One outbound, one recorder.

``server=None`` is accepted for a tokenless/socketless boot: the mirror then records
without broadcasting.
"""

import logging
from typing import Protocol

from ..client_plane import (
    SocketServer,
    card_frame,
    card_resolved_frame,
    file_frame,
    outbound_frame,
)
from ..gate.approvals import ApprovalCard
from ..persistence.messages import (
    KIND_CARD,
    KIND_CARD_RESOLVED,
    KIND_FILE,
    ROLE_CHIEF,
    MessageLog,
)
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
    """Wrap a chat platform IO: deliver first, then broadcast + log the outbound (#133).

    Never wraps the CLI stack — that stack's ``CliTaskIO`` is its own broadcaster and
    its own recorder (see the module docstring).
    """

    def __init__(
        self,
        inner: PlatformIO,
        *,
        platform: str,
        log: MessageLog,
        server: SocketServer | None,
    ) -> None:
        self._inner = inner
        self._platform = platform
        self._log = log
        #: ``None`` when this boot has no client-plane socket — log-only mirroring.
        self._server = server
        #: msg_ref → (route, approval_id), so ``edit_card`` (which sees only the ref)
        #: can name the approval in the card_resolved frame (#136). Popped on edit, so
        #: it stays bounded by the live cards.
        self._cards: dict[str, tuple[str, int]] = {}

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
            kind=KIND_FILE,
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

        The row is ``ROLE_CHIEF`` — the same outbound role the CLI recorder writes, so
        the read model (#134) sees one role per direction across every platform. It
        carries no ``payload``: a chat message's delivery already happened on the chat
        platform, so there is nothing for a client attach to replay, and the row is
        recorded ``delivered=True`` (the ``record`` default) so a replay claim never
        sweeps it.

        Wrapped in a blanket ``except`` on purpose: mirroring is a best-effort side
        channel, so a broadcast or DB failure must never break — or reorder — the
        platform delivery that already happened before this call.
        """
        try:
            if self._server is not None:
                await self._server.broadcast(frame)
            await self._log.record(
                platform=self._platform,
                thread_key=str(frame["thread_key"]),
                role=ROLE_CHIEF,
                kind=kind,
                text=text,
                filename=filename,
            )
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
        """Post the card on the platform, then mirror it onto the socket (#136).

        Platform first (the #133 order): the phone card is the primary surface and its
        ref is the manager's edit handle. The ref → (route, approval_id) memo is what
        lets :meth:`edit_card` — which sees only the ref — name the approval in the
        card_resolved frame; it is popped on edit, so it stays bounded by the live
        cards.
        """
        msg_ref = await self._inner.send_card(route, card)
        self._cards[msg_ref] = (route, card.approval_id)
        await self._mirror(
            card_frame(route, card.approval_id, card.text, platform=self._platform),
            kind=KIND_CARD,
            text=card.text,
            filename=None,
        )
        return msg_ref

    async def edit_card(self, msg_ref: str, text: str) -> None:
        """Edit the platform card, then mirror the outcome onto the socket (#136).

        An unknown ref (a card posted before a restart, re-armed with no memo) edits the
        platform and skips the mirror — there is no approval id to name.
        """
        await self._inner.edit_card(msg_ref, text)
        known = self._cards.pop(msg_ref, None)
        if known is None:
            return
        route, approval_id = known
        await self._mirror(
            card_resolved_frame(route, approval_id, text, platform=self._platform),
            kind=KIND_CARD_RESOLVED,
            text=text,
            filename=None,
        )

    async def send_budget_card(self, route: str, card: BudgetCard) -> None:
        """Delegate — budget traffic stays unmirrored (no mirroring)."""
        await self._inner.send_budget_card(route, card)
