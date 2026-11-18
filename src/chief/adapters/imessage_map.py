"""Map one polled ``chat.db`` row to the Message chief delivers, or skip it.

Split from ``imessage.py`` (the poll loop) to keep it under the length cap:
this is the pure part — who the sender is, which thread, whether it is
chief's own echo — with no I/O.
"""

from chief.adapters.base import Message
from chief.adapters.imessage_store import sent_at

BOT_PREFIX = "\U0001f916 "  # 🤖 — marks chief's replies in the shared self-chat


def map_row(
    channel: str, sender: str, text: str, from_me: int, group_chat: str | None,
    in_self: int, date: int, owner_handles: frozenset[str], dedicated: bool,
) -> Message | None:
    """Turn a polled row into a deliverable Message, or None to skip it.

    A group message threads on its chat, not on whoever spoke — the one
    case where ``sender`` and ``thread_key`` differ. Groups are never the
    owner's self-chat, so they take the stranger path: published for
    monitors, never answered. Which groups matter is monitor policy."""
    if not text:
        return None  # attachment-only row: attributedBody held no text
    if text.startswith(BOT_PREFIX) and not dedicated:
        return None  # chief's own reply echoing back through the store
    if from_me and not in_self:
        return None  # owner->friend sent copy: not the self-chat
    at = sent_at(date)  # when it was sent, not polled: a backlog is old news
    if group_chat is not None:
        # Raw sender, never "owner", even for an owner handle: that would
        # take the dispatcher's owner path, replying to a group thread_key
        # the one-to-one send path cannot address — and would let anyone in
        # the group present as the owner (docs/SECURITY.md).
        return Message(
            channel=channel, sender=sender, thread_key=group_chat,
            text=text, at=at,
        )
    mapped = "owner" if (in_self or sender in owner_handles) else sender
    return Message(
        channel=channel, sender=mapped, thread_key=sender, text=text, at=at
    )
