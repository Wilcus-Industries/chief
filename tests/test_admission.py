"""Guest admission + abuse gate: decide_guest, apply_admission, parse_admission."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.base import (
    AdmissionAction,
    GuestAction,
    Message,
    Tier,
    admission_payload,
    apply_admission,
    decide_guest,
    parse_admission,
)
from chief.persistence import contacts as contact_repo
from chief.persistence.contacts import get_or_create_contact

_LIMITS = {"rate_limit": 5, "rate_window_seconds": 3600, "global_limit": 100}


def _msg(user_id: int = 7) -> Message:
    return Message(
        platform="telegram",
        sender_id=user_id,
        text="hello?",
        thread_key=f"{user_id}:0",
        tier=Tier.GUEST,
        sender_name="Alice",
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession], state: str, user_id: int = 7
) -> int:
    async with session_factory() as session:
        contact = await get_or_create_contact(
            session,
            platform="telegram",
            user_id=str(user_id),
            tier="guest",
            display_name="Alice",
        )
        await contact_repo.set_contact_state(session, contact, state)
        return contact.id


# ---- parse_admission ---------------------------------------------------------


def test_admission_payload_round_trips() -> None:
    payload = admission_payload(42, AdmissionAction.ADMIT)
    assert payload == "adm:42:admit"
    assert parse_admission(payload) == (42, AdmissionAction.ADMIT)


def test_parse_admission_rejects_foreign_or_malformed() -> None:
    assert parse_admission("appr:1:approve_once") is None
    assert parse_admission("adm:1") is None
    assert parse_admission("adm:notanint:admit") is None
    assert parse_admission("adm:1:bogus") is None


# ---- apply_admission ---------------------------------------------------------


async def test_apply_admission_admit_sets_admitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _seed(session_factory, contact_repo.STATE_PENDING)

    contact = await apply_admission(
        session_factory, contact_id=contact_id, action=AdmissionAction.ADMIT
    )

    assert contact is not None and contact.state == contact_repo.STATE_ADMITTED


async def test_apply_admission_block_sets_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    contact_id = await _seed(session_factory, contact_repo.STATE_PENDING)

    await apply_admission(
        session_factory, contact_id=contact_id, action=AdmissionAction.BLOCK
    )

    async with session_factory() as session:
        contact = await contact_repo.get_contact(
            session, platform="telegram", user_id="7"
        )
    assert contact is not None and contact.state == contact_repo.STATE_BLOCKED


async def test_apply_admission_missing_contact_is_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await apply_admission(
            session_factory, contact_id=999, action=AdmissionAction.ADMIT
        )
        is None
    )


# ---- decide_guest ------------------------------------------------------------


async def test_pending_first_contact_prompts_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, contact_repo.STATE_PENDING)

    decision = await decide_guest(session_factory, message=_msg(), **_LIMITS)

    assert decision.action is GuestAction.ADMIT


async def test_admitted_guest_dispatches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, contact_repo.STATE_ADMITTED)

    decision = await decide_guest(session_factory, message=_msg(), **_LIMITS)

    assert decision.action is GuestAction.DISPATCH


async def test_blocked_guest_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, contact_repo.STATE_BLOCKED)

    decision = await decide_guest(session_factory, message=_msg(), **_LIMITS)

    assert decision.action is GuestAction.IGNORE


async def test_muted_guest_relays(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, contact_repo.STATE_MUTED)

    decision = await decide_guest(session_factory, message=_msg(), **_LIMITS)

    assert decision.action is GuestAction.RELAY


async def test_per_guest_rate_limit_drops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_factory, contact_repo.STATE_ADMITTED)
    limits = {"rate_limit": 2, "rate_window_seconds": 3600, "global_limit": 100}

    actions = [
        (await decide_guest(session_factory, message=_msg(), **limits)).action
        for _ in range(3)
    ]

    assert actions == [GuestAction.DISPATCH, GuestAction.DISPATCH, GuestAction.DROP]


async def test_global_budget_drop_flags_over_global(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Two different admitted guests share the global budget of 2.
    await _seed(session_factory, contact_repo.STATE_ADMITTED, user_id=7)
    await _seed(session_factory, contact_repo.STATE_ADMITTED, user_id=8)
    limits = {"rate_limit": 100, "rate_window_seconds": 3600, "global_limit": 2}

    a = await decide_guest(session_factory, message=_msg(7), **limits)
    b = await decide_guest(session_factory, message=_msg(8), **limits)
    c = await decide_guest(session_factory, message=_msg(7), **limits)

    assert (a.action, b.action) == (GuestAction.DISPATCH, GuestAction.DISPATCH)
    assert c.action is GuestAction.DROP and c.over_global is True


async def test_blocked_guest_skips_rate_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A blocked sender is ignored before the rate check — it never consumes budget.
    await _seed(session_factory, contact_repo.STATE_BLOCKED)
    limits = {"rate_limit": 1, "rate_window_seconds": 3600, "global_limit": 1}

    for _ in range(5):
        decision = await decide_guest(session_factory, message=_msg(), **limits)
        assert decision.action is GuestAction.IGNORE
