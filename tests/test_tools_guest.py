"""The guest receptionist tools (chief.tools.guest): leave_message + manage_guest."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import contacts as contact_repo
from chief.persistence.contacts import get_or_create_contact
from chief.tools.guest import (
    GuestAdminService,
    GuestService,
)


def test_leave_message_tool_name_is_sdk_qualified() -> None:
    relays: list[str] = []

    async def relay(text: str) -> None:
        relays.append(text)

    service = GuestService(relay=relay, from_label="Alice")
    assert service.tool_name == "mcp__chief_guest__leave_message"


async def test_leave_message_relays_with_sender_label() -> None:
    relayed: list[str] = []

    async def relay(text: str) -> None:
        relayed.append(text)

    service = GuestService(relay=relay, from_label="Alice")
    tool = service._build_tool()

    out = await tool.handler({"message": "Can we meet Thursday?"})

    assert out["is_error"] is False
    assert len(relayed) == 1
    # The relayed text carries the sender identity (baked, not model-given) + the body.
    assert "Alice" in relayed[0]
    assert "Can we meet Thursday?" in relayed[0]


def test_manage_guest_tool_is_on_a_separate_server() -> None:
    # Critical isolation: the owner admin tool must NOT share the guest server, or a
    # guest session wiring chief_guest would inherit manage_guest.
    admin = GuestAdminService(session_factory=None, platform="telegram")  # type: ignore[arg-type]
    assert admin.server_name == "chief_guest_admin"
    assert admin.tool_name == "mcp__chief_guest_admin__manage_guest"
    assert admin.server_name != GuestService.server_name


async def test_manage_guest_blocks_single_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await get_or_create_contact(
            session,
            platform="telegram",
            user_id="1",
            tier="guest",
            display_name="Alice Smith",
        )

    admin = GuestAdminService(session_factory=session_factory, platform="telegram")
    out = await admin._build_tool().handler({"name": "alice", "action": "block"})

    assert out["is_error"] is False
    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="telegram", user_id="1"
        )
    assert contact is not None
    assert contact.state == contact_repo.STATE_BLOCKED


async def test_manage_guest_unblock_restores_admitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        contact = await get_or_create_contact(
            session,
            platform="telegram",
            user_id="1",
            tier="guest",
            display_name="Alice",
        )
        await contact_repo.set_contact_state(
            session, contact, contact_repo.STATE_BLOCKED
        )

    admin = GuestAdminService(session_factory=session_factory, platform="telegram")
    await admin._build_tool().handler({"name": "alice", "action": "unblock"})

    async with session_factory() as session:
        reloaded = await contact_repo.get_contact(
            session, platform="telegram", user_id="1"
        )
    assert reloaded is not None
    assert reloaded.state == contact_repo.STATE_ADMITTED


async def test_manage_guest_ambiguous_returns_candidates_no_change(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for uid, name in (("1", "Alice Smith"), ("2", "Alice Jones")):
            await get_or_create_contact(
                session,
                platform="telegram",
                user_id=uid,
                tier="guest",
                display_name=name,
            )

    admin = GuestAdminService(session_factory=session_factory, platform="telegram")
    out = await admin._build_tool().handler({"name": "alice", "action": "block"})

    text = out["content"][0]["text"]
    assert "Alice Smith" in text and "Alice Jones" in text
    async with session_factory() as session:
        for uid in ("1", "2"):
            contact = await contact_repo.get_contact(
                session, platform="telegram", user_id=uid
            )
            assert contact is not None
            assert contact.state == contact_repo.STATE_PENDING  # unchanged


async def test_manage_guest_no_match(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = GuestAdminService(session_factory=session_factory, platform="telegram")
    out = await admin._build_tool().handler({"name": "nobody", "action": "block"})

    assert out["is_error"] is False
    assert "nobody" in out["content"][0]["text"].lower()


async def test_manage_guest_rejects_unknown_action(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = GuestAdminService(session_factory=session_factory, platform="telegram")
    out = await admin._build_tool().handler({"name": "alice", "action": "explode"})

    assert out["is_error"] is True
