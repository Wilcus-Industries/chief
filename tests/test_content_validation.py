"""Content validation: skill/package files must be well-formed.

This runs inside the self-edit done-check, so a bogus write (e.g. a
``SKILL.md`` written as the literal ``...``) fails pytest and is rolled
back instead of merged (issue #186).
"""

from pathlib import Path

from chief import classifiers, pkg, skills

REPO = Path(__file__).parent.parent

GOOD_SKILL = """---
name: greet
description: Greet someone warmly.
---
# Greeting

Say hello.
"""

GOOD_CLASSIFIER = """---
name: mood
description: Judge the mood of a message.
labels: [HAPPY, SAD]
---
Reply with the mood of the message.
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


def test_validate_checks_a_lowercase_skill_file(tmp_path: Path) -> None:
    # A mis-cased skill.md must still be validated, not silently skipped —
    # the done-check would otherwise pass over a malformed skill on Linux.
    skill_dir = tmp_path / "hollow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text("---\nname: x\ndescription: y\n---\n\n")
    problems = skills.validate(tmp_path)
    assert any("body" in p for p in problems)


def test_valid_manifest_passes(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "good"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.yaml").write_text("name: good\ndescription: a package\n")
    assert pkg.validate((tmp_path,)) == []


def test_manifest_missing_description_is_flagged(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "bad"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.yaml").write_text("name: bad\n")
    problems = pkg.validate((tmp_path,))
    assert any("description" in p for p in problems)


def test_invalid_manifest_yaml_is_flagged(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "broken"
    pkg_dir.mkdir()
    (pkg_dir / "manifest.yaml").write_text("name: [unterminated\n")
    problems = pkg.validate((tmp_path,))
    assert any("yaml" in p.lower() for p in problems)


def test_valid_classifier_passes(tmp_path: Path) -> None:
    (tmp_path / "mood.md").write_text(GOOD_CLASSIFIER)
    assert classifiers.validate(tmp_path) == []


def test_classifier_missing_labels_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "bare.md").write_text(
        "---\nname: x\ndescription: y\n---\nbody\n"
    )
    problems = classifiers.validate(tmp_path)
    assert any("labels" in p for p in problems)


def test_bundled_classifiers_are_wellformed() -> None:
    problems = classifiers.validate(REPO / "classifiers")
    assert problems == [], "\n".join(problems)


def test_bundled_skills_are_wellformed() -> None:
    problems = skills.validate(REPO / "skills")
    for pkg_skills in sorted((REPO / "packages").glob("*/skills")):
        problems += skills.validate(pkg_skills)
    assert problems == [], "\n".join(problems)


def test_bundled_manifests_are_wellformed() -> None:
    assert pkg.validate((REPO / "packages",)) == []
