"""Base prompt loading and the per-turn soul reader."""

from pathlib import Path

from chief.agent.prompt import DEFAULT_SYSTEM_PROMPT, read_soul, system_prompt


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
