"""Content validation: skill/package files must be well-formed.

This runs inside the self-edit done-check, so a bogus write (e.g. a
``SKILL.md`` written as the literal ``...``) fails pytest and is rolled
back instead of merged (issue #186).
"""

from pathlib import Path

from chief import packages, skills

REPO = Path(__file__).parent.parent

GOOD_SKILL = """---
name: greet
description: Greet someone warmly.
---
# Greeting

Say hello.
"""


def write_skill(root: Path, name: str, text: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text)


def test_valid_skill_passes(tmp_path: Path) -> None:
    write_skill(tmp_path, "greet", GOOD_SKILL)
    assert skills.validate(tmp_path) == []


def test_missing_frontmatter_is_flagged(tmp_path: Path) -> None:
    write_skill(tmp_path, "bogus", "...")
    problems = skills.validate(tmp_path)
    assert len(problems) == 1
    assert "frontmatter" in problems[0]


def test_missing_name_and_description_are_flagged(tmp_path: Path) -> None:
    write_skill(tmp_path, "bare", "---\nother: 1\n---\nbody\n")
    problems = skills.validate(tmp_path)
    assert any("name" in p for p in problems)
    assert any("description" in p for p in problems)


def test_empty_body_is_flagged(tmp_path: Path) -> None:
    write_skill(tmp_path, "hollow", "---\nname: x\ndescription: y\n---\n\n")
    problems = skills.validate(tmp_path)
    assert any("body" in p for p in problems)


def test_unclosed_frontmatter_is_flagged(tmp_path: Path) -> None:
    write_skill(tmp_path, "open", "---\nname: x\ndescription: y\n")
    problems = skills.validate(tmp_path)
    assert len(problems) == 1


def test_valid_manifest_passes(tmp_path: Path) -> None:
    pkg = tmp_path / "good"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text("name: good\ndescription: a package\n")
    assert packages.validate((tmp_path,)) == []


def test_manifest_missing_description_is_flagged(tmp_path: Path) -> None:
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text("name: bad\n")
    problems = packages.validate((tmp_path,))
    assert any("description" in p for p in problems)


def test_invalid_manifest_yaml_is_flagged(tmp_path: Path) -> None:
    pkg = tmp_path / "broken"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text("name: [unterminated\n")
    problems = packages.validate((tmp_path,))
    assert any("yaml" in p.lower() for p in problems)


def test_bundled_skills_are_wellformed() -> None:
    problems = skills.validate(REPO / "skills")
    for pkg_skills in sorted((REPO / "packages").glob("*/skills")):
        problems += skills.validate(pkg_skills)
    assert problems == [], "\n".join(problems)


def test_bundled_manifests_are_wellformed() -> None:
    assert packages.validate((REPO / "packages",)) == []
