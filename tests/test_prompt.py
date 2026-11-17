"""Base prompt loading and the per-turn soul reader."""

from pathlib import Path

import pytest

from chief.agent.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    boot_prompt,
    read_soul,
    system_prompt,
)
from chief.agent.timestamps import STAMP_NOTE
from chief.persistence.store import MessageStore


def test_default_when_no_file(tmp_path: Path) -> None:
    assert system_prompt(path=tmp_path / "system.md") == DEFAULT_SYSTEM_PROMPT


def test_editable_file_overrides_default(tmp_path: Path) -> None:
    sys_md = tmp_path / "system.md"
    sys_md.write_text("You are a custom chief.")
    assert system_prompt(path=sys_md) == "You are a custom chief."


def test_empty_file_falls_back_to_default(tmp_path: Path) -> None:
    sys_md = tmp_path / "system.md"
    sys_md.write_text("   \n")
    assert system_prompt(path=sys_md) == DEFAULT_SYSTEM_PROMPT


def test_system_prompt_never_includes_soul(tmp_path: Path) -> None:
    # Soul is a per-turn concern (read_soul), not baked into the boot prompt.
    sys_md = tmp_path / "system.md"
    sys_md.write_text("You are chief.")
    assert system_prompt(path=sys_md) == "You are chief."


def test_read_soul_returns_stripped_text(tmp_path: Path) -> None:
    soul = tmp_path / "Soul.md"
    soul.write_text("\n# Soul\n\nI am candid.\n")
    assert read_soul(soul_path=soul) == "# Soul\n\nI am candid."


def test_read_soul_absent_is_empty(tmp_path: Path) -> None:
    assert read_soul(soul_path=tmp_path / "Soul.md") == ""


def test_read_soul_empty_file_is_empty(tmp_path: Path) -> None:
    soul = tmp_path / "Soul.md"
    soul.write_text("   \n")
    assert read_soul(soul_path=soul) == ""


def test_read_soul_non_utf8_is_empty_not_crash(tmp_path: Path) -> None:
    # A hand-edited soul with odd bytes must never crash the daemon at read.
    soul = tmp_path / "Soul.md"
    soul.write_bytes(b"\xff\xfe not valid utf-8 \x80")
    assert read_soul(soul_path=soul) == ""


def test_read_soul_directory_is_empty_not_crash(tmp_path: Path) -> None:
    soul = tmp_path / "Soul.md"
    soul.mkdir()
    assert read_soul(soul_path=soul) == ""


def test_default_prompt_teaches_verify_before_explain() -> None:
    # Anti-confabulation rule: check the mechanism (source, audit log) before
    # explaining a failure of chief's own tooling — never invent one.
    assert "verify the mechanism" in DEFAULT_SYSTEM_PROMPT


def test_the_stamp_note_says_the_stamp_is_host_added_and_not_to_be_written() -> None:
    # Messages arrive stamped by the host; the model must not echo the stamp
    # into its own replies (a transcript full of stamps invites imitating one).
    assert "host-added local-time stamp" in STAMP_NOTE
    assert "never write one yourself" in STAMP_NOTE


async def test_a_box_on_the_default_prompt_is_told_about_the_stamp_once(
    store: MessageStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no data/system.md here: the built-in default

    prompt = await boot_prompt(store, "")

    assert prompt.startswith(DEFAULT_SYSTEM_PROMPT)
    assert prompt.count(STAMP_NOTE) == 1  # not also inside the default itself


async def test_a_box_with_its_own_system_md_is_still_told_about_the_stamp(
    store: MessageStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The note is the host's, not the persona's: replacing the base prompt must
    # not leave the model reading stamps nobody explained.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "system.md").write_text("You are a custom chief.")

    prompt = await boot_prompt(store, "\n\nSkills: none")

    assert prompt.startswith("You are a custom chief.")
    assert prompt.count(STAMP_NOTE) == 1
    assert prompt.index(STAMP_NOTE) < prompt.index("Skills: none")
