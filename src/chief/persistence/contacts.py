"""Contact repository — get-or-create on first contact.

Stamps a per-contact memory ``namespace`` and the sender's tier so later milestones
(admission in M6, memory namespacing in M4) have a stable record to build on.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Contact


async def get_or_create_contact(
    session: AsyncSession,
    *,
    platform: str,
    user_id: str,
    tier: str,
    display_name: str | None = None,
) -> Contact:
    """Return the existing contact for ``(platform, user_id)`` or create it."""
    stmt = select(Contact).where(
        Contact.platform == platform, Contact.user_id == user_id
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    contact = Contact(
        platform=platform,
        user_id=user_id,
        display_name=display_name,
        tier=tier,
        namespace=f"{platform}:{user_id}",
    )
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return contact
