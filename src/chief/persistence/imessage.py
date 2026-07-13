"""iMessage whitelist, prefs, cursor, and unknown-sender repository (#156).

The whitelist *is* the event gate: a handle raises adapter events only when a
``Contact`` row exists for it on the ``imessage`` platform. Owner handles are
seeded owner-tier at setup (:func:`seed_owner_handles` — the seam the installer
wizard calls); further handles are added by the owner (chat tool or the web
settings page) and enter as guests through the existing guest machinery. A sender
with no row gets no session and no reply — only a content-free
:class:`~chief.persistence.models.UnknownSender` line (handle + timestamps).

Per-handle prefs (:class:`~chief.persistence.models.IMessagePref`) carry the
conversation's delegation mode — ``auto`` (default) or ``draft`` (draft-first
approval of every outbound) — and the ``contacted`` first-send flag. The poll
cursor (:class:`~chief.persistence.models.AdapterCursor`) persists the last
processed ``message.ROWID`` so restarts neither replay nor drop.
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import contacts as contact_repo
from .models import AdapterCursor, Contact, IMessagePref, UnknownSender

#: The adapter's platform key (`Contact.platform`, thread queries, the cursor row).
PLATFORM = "imessage"

#: Delegation modes for guest conversations (``IMessagePref.mode``).
MODE_AUTO = "auto"  # chief replies freely within its guest-tier limits
MODE_DRAFT = "draft"  # every outbound parks on an owner approval card first

#: Whitelist tiers (``Contact.tier`` on the imessage platform).
TIER_OWNER = "owner"
TIER_GUEST = "guest"


def normalize_handle(handle: str) -> str:
    """Canonicalize a Messages handle for whitelist matching.

    Emails lowercase; phone numbers lose separators (spaces, dashes, parens, dots)
    but keep a leading ``+`` — the store writes E.164, so whitelist entries should
    be E.164 (or an email) too.
    """
    handle = handle.strip()
    if "@" in handle:
        return handle.lower()
    return "".join(ch for ch in handle if ch.isdigit() or ch == "+")


# ---- whitelist -------------------------------------------------------------------


async def add_handle(
    session: AsyncSession,
    *,
    handle: str,
    tier: str = TIER_GUEST,
    mode: str = MODE_AUTO,
) -> Contact:
    """Whitelist ``handle`` (idempotent): the owner's explicit add IS the admission.

    Creates (or updates) the imessage ``Contact`` at ``tier`` in the ``admitted``
    state — the owner saying "listen to Mom" already decided admission, so no
    admission card re-asks — plus its pref row at ``mode``. An existing entry is
    upgraded in place (tier and mode), never duplicated. The handle also leaves the
    unknown-senders log: it is known now.
    """
    handle = normalize_handle(handle)
    contact = await contact_repo.get_or_create_contact(
        session, platform=PLATFORM, user_id=handle, tier=tier, display_name=handle
    )
    contact.tier = tier
    await contact_repo.set_contact_state(
        session, contact, contact_repo.STATE_ADMITTED
    )
    pref = await get_pref(session, handle)
    if pref is None:
        pref = IMessagePref(handle=handle, mode=mode)
        session.add(pref)
    else:
        pref.mode = mode
    # Owner handles never draft-gate; mark them contacted so the first-send card
    # (a guest-only guard) can never fire against the owner's own thread.
    if tier == TIER_OWNER:
        pref.contacted = True
    await session.commit()
    await session.execute(
        delete(UnknownSender).where(
            UnknownSender.platform == PLATFORM, UnknownSender.handle == handle
        )
    )
    await session.commit()
    return contact


async def remove_handle(session: AsyncSession, handle: str) -> bool:
    """Drop ``handle`` from the whitelist entirely (contact + pref rows).

    A removed handle is unknown again: its texts raise no events and fall back to
    the metadata-only log. Returns ``False`` when the handle wasn't whitelisted.
    """
    handle = normalize_handle(handle)
    contact = await contact_repo.get_contact(
        session, platform=PLATFORM, user_id=handle
    )
    if contact is None:
        return False
    await session.delete(contact)
    await session.execute(
        delete(IMessagePref).where(IMessagePref.handle == handle)
    )
    await session.commit()
    return True


async def list_whitelist(
    session: AsyncSession,
) -> list[tuple[Contact, IMessagePref | None]]:
    """Every whitelisted handle with its pref row (mode/contacted), owner first."""
    contacts = list(
        (
            await session.execute(
                select(Contact)
                .where(Contact.platform == PLATFORM)
                .order_by(Contact.tier.desc(), Contact.id)
            )
        ).scalars()
    )
    prefs = {
        pref.handle: pref
        for pref in (await session.execute(select(IMessagePref))).scalars()
    }
    return [(contact, prefs.get(contact.user_id)) for contact in contacts]


async def seed_owner_handles(
    session_factory: async_sessionmaker[AsyncSession],
    handles: tuple[str, ...] | list[str],
) -> None:
    """Seed the owner's handles owner-tier (idempotent) — the setup/wizard seam.

    Called at adapter boot from ``settings.imessage_owner_handles`` and directly
    by the #154 installer wizard once it knows the owner's number/email. An
    existing guest-tier row for the same handle is upgraded to owner.
    """
    async with session_factory() as session:
        for handle in handles:
            await add_handle(session, handle=handle, tier=TIER_OWNER)


# ---- per-handle prefs --------------------------------------------------------------


async def get_pref(session: AsyncSession, handle: str) -> IMessagePref | None:
    """The pref row for ``handle`` (normalized), or ``None``."""
    stmt = select(IMessagePref).where(
        IMessagePref.handle == normalize_handle(handle)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_mode(
    session: AsyncSession, handle: str, mode: str
) -> IMessagePref | None:
    """Persist the conversation's delegation mode; ``None`` if not whitelisted."""
    handle = normalize_handle(handle)
    contact = await contact_repo.get_contact(
        session, platform=PLATFORM, user_id=handle
    )
    if contact is None:
        return None
    pref = await get_pref(session, handle)
    if pref is None:
        pref = IMessagePref(handle=handle, mode=mode)
        session.add(pref)
    else:
        pref.mode = mode
    await session.commit()
    return pref


async def mark_contacted(session: AsyncSession, handle: str) -> None:
    """Record that chief has now texted ``handle`` (the first-send card is spent)."""
    handle = normalize_handle(handle)
    pref = await get_pref(session, handle)
    if pref is None:
        pref = IMessagePref(handle=handle, contacted=True)
        session.add(pref)
    else:
        pref.contacted = True
    await session.commit()


# ---- poll cursor -------------------------------------------------------------------


async def get_cursor(session: AsyncSession, platform: str) -> int | None:
    """The persisted poll position for ``platform``, or ``None`` before first boot."""
    stmt = select(AdapterCursor).where(AdapterCursor.platform == platform)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row.position if row is not None else None


async def set_cursor(session: AsyncSession, platform: str, position: int) -> None:
    """Persist the poll position (upsert one row per platform)."""
    stmt = select(AdapterCursor).where(AdapterCursor.platform == platform)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        session.add(AdapterCursor(platform=platform, position=position))
    else:
        row.position = position
    await session.commit()


# ---- unknown senders ---------------------------------------------------------------


async def record_unknown_sender(
    session: AsyncSession, *, platform: str, handle: str, seen_at: datetime
) -> None:
    """Accumulate one metadata-only sighting: handle + timestamps, NEVER content."""
    stmt = select(UnknownSender).where(
        UnknownSender.platform == platform, UnknownSender.handle == handle
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        session.add(
            UnknownSender(
                platform=platform,
                handle=handle,
                first_seen=seen_at,
                last_seen=seen_at,
                count=1,
            )
        )
    else:
        row.count += 1
        if seen_at > row.last_seen:
            row.last_seen = seen_at
    await session.commit()


async def list_unknown_senders(
    session: AsyncSession, *, platform: str
) -> list[UnknownSender]:
    """The "who's texted you?" surface: unknown senders, most recent first."""
    stmt = (
        select(UnknownSender)
        .where(UnknownSender.platform == platform)
        .order_by(UnknownSender.last_seen.desc())
    )
    return list((await session.execute(stmt)).scalars())
