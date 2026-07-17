"""Self-knowledge docs: ARCHITECTURE.md exists and the self-edit skill body
points at it — the always-reachable discovery anchor for self-changes."""

from pathlib import Path

from chief.skills import SkillLibrary

REPO_ROOT = Path(__file__).parent.parent


def test_architecture_doc_exists() -> None:
    assert (REPO_ROOT / "docs" / "ARCHITECTURE.md").is_file()


def test_self_edit_skill_points_to_architecture_doc() -> None:
    skill = SkillLibrary(REPO_ROOT / "skills").get("self-edit")
    assert skill is not None
    assert "docs/ARCHITECTURE.md" in skill.body()
