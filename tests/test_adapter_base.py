"""Platform-neutral types and tier classification."""

from chief.adapters.base import Tier, classify_tier


def test_owner_id_matches_exactly() -> None:
    assert classify_tier(sender_id=42, owner_id=42) is Tier.OWNER


def test_non_owner_is_guest() -> None:
    assert classify_tier(sender_id=7, owner_id=42) is Tier.GUEST


def test_tier_values_are_persistable_strings() -> None:
    # The DB stores tier as a plain string; these are the canonical values.
    assert Tier.OWNER.value == "owner"
    assert Tier.GUEST.value == "guest"
