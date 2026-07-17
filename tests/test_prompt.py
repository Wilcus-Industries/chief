"""System prompt assembly: editable file, default, and inlined Soul.md."""

from pathlib import Path

from chief.agent.prompt import DEFAULT_SYSTEM_PROMPT, system_prompt


def test_default_when_no_files(tmp_path: Path) -> None:
    prompt = system_prompt(
        path=tmp_path / "system.md", soul_path=tmp_path / "Soul.md"
    )
    assert prompt == DEFAULT_SYSTEM_PROMPT


def test_editable_file_overrides_default(tmp_path: Path) -> None:
    sys_md = tmp_path / "system.md"
    sys_md.write_text("You are a custom chief.")
    prompt = system_prompt(path=sys_md, soul_path=tmp_path / "Soul.md")
    assert prompt == "You are a custom chief."


def test_soul_inlined_at_top(tmp_path: Path) -> None:
    sys_md = tmp_path / "system.md"
    sys_md.write_text("You are chief.")
    soul = tmp_path / "Soul.md"
    soul.write_text("# Soul\n\nI am candid and concise.")
    prompt = system_prompt(path=sys_md, soul_path=soul)
    # Soul leads (who you are), then the operating prompt — one blank line apart.
    assert prompt == "# Soul\n\nI am candid and concise.\n\nYou are chief."


def test_soul_inlined_over_default(tmp_path: Path) -> None:
    soul = tmp_path / "Soul.md"
    soul.write_text("I am candid.")
    prompt = system_prompt(path=tmp_path / "system.md", soul_path=soul)
    assert prompt == f"I am candid.\n\n{DEFAULT_SYSTEM_PROMPT}"


def test_empty_soul_ignored(tmp_path: Path) -> None:
    soul = tmp_path / "Soul.md"
    soul.write_text("   \n")
    prompt = system_prompt(path=tmp_path / "system.md", soul_path=soul)
    assert prompt == DEFAULT_SYSTEM_PROMPT
