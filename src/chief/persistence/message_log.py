"""Message-log repository — records every outbound message on a thread (#133).

The #133 broadcast bus mirrors each stack's engine outbound onto the client-plane socket
and records it here. Mirrors ``usage.py``: module-level async fns over an
``AsyncSession``, string consts for the enumerated columns. #132 builds inbound
recording + replay on top of this table.

The ``KIND_*`` values deliberately equal the wire frame ``type`` strings, so the mirror
can pass a frame's ``type`` straight through as the row's ``kind``.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from .models import MessageLogEntry

#: The message author. Only outbound (assistant) rows are recorded now; #132 adds the
#: inbound (owner/guest) roles.
ROLE_ASSISTANT = "assistant"

#: Row kinds — equal to the wire frame ``type`` strings so the mirror passes them along.
KIND_REPLY = "reply"
KIND_MILESTONE = "milestone"
KIND_FILE = "file"


async def record(
    session: AsyncSession,
    *,
    platform: str,
    thread_key: str,
    role: str,
    kind: str,
    text: str,
    filename: str | None = None,
) -> None:
    """Add one message-log row (the caller owns the commit, like ``usage.py``)."""
    session.add(
        MessageLogEntry(
            platform=platform,
            thread_key=thread_key,
            role=role,
            kind=kind,
            text=text,
            filename=filename,
            # Payload-less mirror rows can never replay, so they must never join the
            # claim set a ``claim_replay`` sweeps (the column default stays False as
            # the defensive fallback for #132's outbound rows).
            delivered=True,
        )
    )
