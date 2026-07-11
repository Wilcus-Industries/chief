"""The per-thread message log — record every stack's traffic + detach-replay (#132).

The one repository over the ``message_log`` table, and the future dashboard read model
(#128): every inbound owner message and every outbound chief frame — CLI (#132) *and*
the chat stacks' broadcast-bus mirror (#133) — lands one
:class:`~chief.persistence.models.MessageLogEntry` row through
:meth:`MessageLog.record`. There is no separate outbox — the log *is* the held-message
mechanism. An outbound row snapshots ``delivered`` from whether a client was attached at
emit time; :meth:`MessageLog.claim_replay` on the next attach fetches the undelivered
rows, marks them all delivered in one transaction, and returns the most-recent window of
decoded wire frames in id order. That single claim under a lock is what guarantees a
second reattach re-delivers nothing.

**One writer per outbound message.** A stack's outbound is recorded by exactly one
recorder — the CLI stack's :class:`~chief.adapters.cli.CliTaskIO` (which owns the
payload + ``delivered`` snapshot replay needs), the chat stacks' mirror — so the log
never doubles a message. All of them use ``ROLE_CHIEF``: one outbound role across every
platform, so a reader (#134) selects on one value.

Roles and kinds are plain-string constants (persistence stays adapter-independent, same
rule as ``tier`` in :mod:`chief.persistence.models`), so no enum or wire-protocol module
is imported from the adapter layer. DI mirrors
:class:`~chief.gate.approvals.ApprovalManager`: the repo holds the session factory.
"""

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import MessageLogEntry

#: Who authored a logged message. Plain strings, not an adapter enum, so persistence
#: stays independent of the adapter layer (same rule as ``tier``).
ROLE_OWNER = "owner"  # an inbound message from the owner
ROLE_CHIEF = "chief"  # an outbound message from chief — every platform, one role

#: Row kinds — deliberately equal to the wire frame ``type`` strings, so a recorder can
#: pass a frame's ``type`` straight through as the row's ``kind`` (declared here rather
#: than imported from ``client_plane`` to keep persistence protocol-independent).
KIND_REPLY = "reply"
KIND_MILESTONE = "milestone"
KIND_FILE = "file"
KIND_CARD = "card"
KIND_CARD_RESOLVED = "card_resolved"

#: The most-recent window of undelivered frames replayed on attach (#132). A module
#: constant, not a config knob — right-sized here; #134 can make it configurable.
REPLAY_LIMIT = 100


class MessageLog:
    """Record both directions of a thread's traffic and replay held frames (#132)."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory
        #: Serializes claims so two attaches racing on the same platform can't both
        #: fetch the same undelivered rows and double-replay them.
        self._lock = asyncio.Lock()

    async def record(
        self,
        *,
        platform: str,
        thread_key: str,
        role: str,
        kind: str,
        text: str,
        surface: str | None = None,
        filename: str | None = None,
        payload: str | None = None,
        delivered: bool = True,
    ) -> None:
        """Append one logged message. ``payload`` is the outbound wire-frame JSON — the
        replay source — and is ``None`` for inbound rows and for the chat stacks' mirror
        rows (#133), neither of which ever replays onto a client.

        ``delivered`` is the caller's snapshot: an outbound row passes ``False`` when no
        client was attached at emit, so :meth:`claim_replay` picks it up on the next
        attach; inbound rows, direct command replies, and payload-less mirror rows pass
        ``True`` (already live, or nothing to re-emit). ``surface`` and ``filename`` are
        the per-producer columns: the CLI recorder fills ``surface``, the mirror fills
        ``filename`` on file rows.
        """
        async with self._session_factory() as session:
            session.add(
                MessageLogEntry(
                    platform=platform,
                    thread_key=thread_key,
                    role=role,
                    surface=surface,
                    kind=kind,
                    text=text,
                    filename=filename,
                    payload=payload,
                    delivered=delivered,
                )
            )
            await session.commit()

    async def claim_replay(
        self, *, platform: str, limit: int = REPLAY_LIMIT
    ) -> list[dict[str, object]]:
        """Claim every undelivered outbound frame for ``platform``; return a window.

        Under the claim lock, in one transaction: fetch the undelivered rows in id
        order, mark **all** of them delivered (even those beyond ``limit``), commit,
        then decode the last ``limit`` rows' payloads into wire frames. Marking all —
        not just the returned window — is what makes a second reattach re-deliver
        nothing. Rows with no payload (defensive) or a non-object decode are still
        claimed but yield no frame.
        """
        async with self._lock:
            async with self._session_factory() as session:
                stmt = (
                    select(MessageLogEntry)
                    .where(
                        MessageLogEntry.platform == platform,
                        MessageLogEntry.delivered.is_(False),
                    )
                    .order_by(MessageLogEntry.id)
                )
                rows = (await session.execute(stmt)).scalars().all()
                for row in rows:
                    row.delivered = True
                await session.commit()
                # Decode only the most-recent window, ascending by id.
                frames: list[dict[str, object]] = []
                for row in rows[-limit:]:
                    if row.payload is None:
                        continue
                    frame = json.loads(row.payload)
                    if isinstance(frame, dict):
                        frames.append(frame)
                return frames
