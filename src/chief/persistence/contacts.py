"""Contact repository — get-or-create on first contact + admission state.

Stamps a per-contact memory ``namespace`` and the sender's tier so later milestones
(admission in M6, memory namespacing in M4) have a stable record to build on. M6 adds
the admission/abuse ``state`` machine (pending → admitted, or blocked/muted) and a name
lookup for the owner's ``manage_guest`` tool.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Contact

#: Admission/abuse states for ``Contact.state``.
STATE_PENDING = "pending"  # first contact, not yet admitted by the owner
STATE_ADMITTED = "admitted"  # owner allowed this sender to use the receptionist
STATE_BLOCKED = "blocked"  # ignore entirely (no reply, no relay)
STATE_MUTED = "muted"  # take messages silently (relay, no reply)


async def get_contact(
    session: AsyncSession, *, platform: str, user_id: str
) -> Contact | None:
    """Return the contact for ``(platform, user_id)`` or ``None`` (pure read)."""
    stmt = select(Contact).where(
        Contact.platform == platform, Contact.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_contact(
    session: AsyncSession,
    *,
    platform: str,
    user_id: str,
    tier: str,
    display_name: str | None = None,
) -> Contact:
    """Return the existing contact for ``(platform, user_id)`` or create it."""
    existing = await get_contact(session, platform=platform, user_id=user_id)
    if existing is not None:
        return existing

    contact = Contact(
        platform=platform,
        user_id=user_id,
        display_name=display_name,
        tier=tier,
        state=STATE_PENDING,
        namespace=f"{platform}:{user_id}",
    )
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact


async def set_contact_state(
    session: AsyncSession, contact: Contact, state: str
) -> None:
    """Persist a new admission/abuse ``state`` for ``contact``."""
    contact.state = state
    contact.admitted = state == STATE_ADMITTED
    await session.commit()


async def find_contacts_by_name(
    session: AsyncSession, *, platform: str, name: str
) -> list[Contact]:
    """Return contacts on ``platform`` whose display name contains ``name`` (ci).

    Powers the owner's ``manage_guest`` tool, which resolves a guest by name; an
    ambiguous match returns several so the model can disambiguate.
    """
    pattern = f"%{name.lower()}%"
    stmt = (
        select(Contact)
        .where(
            Contact.platform == platform,
            Contact.display_name.is_not(None),
            func.lower(Contact.display_name).like(pattern),
        )
        .order_by(Contact.id)
    )
    return list((await session.execute(stmt)).scalars())
