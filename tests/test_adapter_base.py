"""Platform-neutral types, tier classification, and message splitting (M8)."""

from chief.adapters.base import (
    Message,
    Surface,
    Tier,
    classify_tier,
    is_engaged,
    reply_filename,
    should_send_as_file,
    split_message,
)
from chief.adapters.commands import owner_registry


def test_owner_id_matches_exactly() -> None:
    assert classify_tier(sender_id=42, owner_id=42) is Tier.OWNER


def test_non_owner_is_guest() -> None:
    assert classify_tier(sender_id=7, owner_id=42) is Tier.GUEST


def test_tier_values_are_persistable_strings() -> None:
    # The DB stores tier as a plain string; these are the canonical values.
    assert Tier.OWNER.value == "owner"
    assert Tier.GUEST.value == "guest"


# ---- Surface + engagement gate (M11 group chats) -----------------------------


def test_surface_values_are_persistable_strings() -> None:
    assert Surface.HOME.value == "home"
    assert Surface.DM.value == "dm"
    assert Surface.GROUP.value == "group"


def test_message_surface_defaults_to_dm() -> None:
    # Existing 1:1 construction sites omit surface; the default keeps them DM.
    msg = Message(
        platform="telegram",
        sender_id=42,
        text="hi",
        thread_key="42:0",
        tier=Tier.OWNER,
    )
    assert msg.surface is Surface.DM


def test_is_engaged_on_mention() -> None:
    assert is_engaged(mentioned=True, replied_to_bot=False) is True


def test_is_engaged_on_reply_to_bot() -> None:
    assert is_engaged(mentioned=False, replied_to_bot=True) is True


def test_not_engaged_when_neither() -> None:
    assert is_engaged(mentioned=False, replied_to_bot=False) is False


def test_is_engaged_on_both() -> None:
    assert is_engaged(mentioned=True, replied_to_bot=True) is True


# ---- split_message: boundary-aware splitting (M8) ----------------------------


def test_split_under_limit_is_single_chunk() -> None:
    assert split_message("hi there", 4096) == ["hi there"]


def test_split_breaks_on_paragraphs() -> None:
    text = "para one is here\n\npara two is here\n\npara three"

    chunks = split_message(text, 20)

    # Splits land on the blank-line boundaries — no paragraph is cut mid-word.
    assert chunks == ["para one is here", "para two is here", "para three"]
    assert all(len(c) <= 20 for c in chunks)


def test_split_breaks_on_word_boundaries() -> None:
    text = "the quick brown fox jumps over the lazy dog"

    chunks = split_message(text, 20)

    assert all(len(c) <= 20 for c in chunks)
    # Every break is at a space — rejoining with one space reproduces the text.
    assert " ".join(chunks) == text


def test_split_keeps_code_fence_intact() -> None:
    code = "```\n" + "\n".join(f"line{i}" for i in range(5)) + "\n```"
    text = "before\n\n" + code + "\n\nafter"

    chunks = split_message(text, len(code) + 2)

    # The whole fenced block lands in one chunk — never split across messages.
    assert code in chunks


def test_split_hard_splits_oversized_word() -> None:
    chunks = split_message("a" * 50, 20)

    assert chunks == ["a" * 20, "a" * 20, "a" * 10]
    assert "".join(chunks) == "a" * 50


# ---- should_send_as_file / reply_filename (M8) -------------------------------


def test_should_send_as_file_on_very_long_text() -> None:
    assert should_send_as_file("x" * 100, 20) is True  # over limit × 4
    assert should_send_as_file("short", 20) is False


def test_should_send_as_file_on_oversized_code_block() -> None:
    # Total is under limit×4 (no length trigger), but the fenced block alone exceeds
    # the limit — it can't be split without breaking the fence, so: file.
    code = "```\n" + "y" * 100 + "\n```"

    assert should_send_as_file(code, 30) is True
    assert should_send_as_file("```\ncode\n```", 2000) is False


def test_reply_filename_is_timestamped_markdown() -> None:
    name = reply_filename()

    assert name.startswith("reply-") and name.endswith(".md")


def test_owner_registry_entries_describe_every_command() -> None:
    # entries() is the one source both clients' autocomplete menus draw from:
    # same names as names(), registration order, and no command left blank.
    registry = owner_registry()

    entries = registry.entries()

    assert [name for name, _ in entries] == registry.names()
    assert all(desc for _, desc in entries)
    assert ("tasks", "List active tasks") in entries
