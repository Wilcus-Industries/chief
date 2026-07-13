"""The owner's iMessage whitelist tools + repo edge cases (#156)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.persistence import imessage as imessage_repo
from chief.persistence.contacts import get_contact
from chief.tools.imessage_admin import IMessageAdminService
from chief.tools.inprocess import InProcessServerConfig


def _handler(config: InProcessServerConfig, name: str) -> Any:
    return next(t for t in config["tools"] if t.name == name).handler


def _text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


async def test_allow_whitelists_a_guest_and_survives_reallow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")

    result = await manage({"handle": "+1 555-000-0002", "action": "allow"})
    again = await manage({"handle": "+15550000002", "action": "allow"})

    assert not result["is_error"] and not again["is_error"]
    async with session_factory() as session:
        contact = await get_contact(
            session, platform=imessage_repo.PLATFORM, user_id="+15550000002"
        )
        assert contact is not None
        assert contact.tier == "guest" and contact.state == "admitted"


async def test_allow_clears_the_unknown_senders_entry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await imessage_repo.record_unknown_sender(
            session,
            platform=imessage_repo.PLATFORM,
            handle="+15550000007",
            seen_at=datetime.now(UTC),
        )
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")

    await manage({"handle": "+15550000007", "action": "allow"})

    async with session_factory() as session:
        unknown = await imessage_repo.list_unknown_senders(
            session, platform=imessage_repo.PLATFORM
        )
    assert unknown == []  # known now — no longer in the strangers log


async def test_draft_and_auto_flip_the_delegation_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")
    await manage({"handle": "+15550000002", "action": "allow"})

    await manage({"handle": "+15550000002", "action": "draft"})
    async with session_factory() as session:
        pref = await imessage_repo.get_pref(session, "+15550000002")
    assert pref is not None and pref.mode == imessage_repo.MODE_DRAFT

    await manage({"handle": "+15550000002", "action": "auto"})
    async with session_factory() as session:
        pref = await imessage_repo.get_pref(session, "+15550000002")
    assert pref is not None and pref.mode == imessage_repo.MODE_AUTO


async def test_mode_change_for_a_non_whitelisted_handle_errors(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")

    result = await manage({"handle": "+15550000008", "action": "draft"})

    assert result["is_error"]
    assert "allow it first" in _text(result)


async def test_remove_takes_the_handle_off_the_whitelist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")
    await manage({"handle": "+15550000002", "action": "allow"})

    result = await manage({"handle": "+15550000002", "action": "remove"})
    missing = await manage({"handle": "+15550000002", "action": "remove"})

    assert "off the whitelist" in _text(result)
    assert "wasn't on the whitelist" in _text(missing)
    async with session_factory() as session:
        assert await imessage_repo.list_whitelist(session) == []


async def test_bad_action_and_missing_handle_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = IMessageAdminService(session_factory=session_factory)
    manage = _handler(service.server_config(), "manage_imessage_handle")

    assert (await manage({"handle": "+1555", "action": "explode"}))["is_error"]
    assert (await manage({"handle": "  ", "action": "allow"}))["is_error"]


async def test_list_shows_tier_mode_and_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await imessage_repo.add_handle(
            session, handle="+15550000001", tier="owner"
        )
        await imessage_repo.add_handle(
            session,
            handle="+15550000002",
            tier="guest",
            mode=imessage_repo.MODE_DRAFT,
        )
    service = IMessageAdminService(session_factory=session_factory)
    listing = _handler(service.server_config(), "list_imessage_handles")

    text = _text(await listing({}))

    assert "+15550000001 — owner" in text
    assert "+15550000002 — guest, draft" in text


async def test_unknown_senders_tool_reports_metadata_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await imessage_repo.record_unknown_sender(
            session,
            platform=imessage_repo.PLATFORM,
            handle="+15550000009",
            seen_at=datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
        )
    service = IMessageAdminService(session_factory=session_factory)
    unknown = _handler(service.server_config(), "unknown_imessage_senders")

    text = _text(await unknown({}))

    assert "+15550000009" in text and "1 message(s)" in text
    empty_service = _text(await unknown({})).count("secret")
    assert empty_service == 0  # nothing but handle + time ever surfaces


async def test_email_handles_normalize_to_lowercase(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert imessage_repo.normalize_handle(" Mom@Example.COM ") == "mom@example.com"
    assert imessage_repo.normalize_handle("+1 (555) 000.0002") == "+15550000002"


async def test_tool_names_cover_all_three_tools() -> None:
    service = IMessageAdminService(session_factory=None)  # type: ignore[arg-type]
    assert service.tool_names == (
        "mcp__chief_imessage_admin__manage_imessage_handle",
        "mcp__chief_imessage_admin__list_imessage_handles",
        "mcp__chief_imessage_admin__unknown_imessage_senders",
    )
