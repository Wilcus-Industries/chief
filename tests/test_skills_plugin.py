"""The chief-skills plugin manifest stays in lockstep with config.default_skills.

Filesystem/manifest guards: they catch a curated skill name that no longer resolves to a
SKILL.md on disk — a typo, a moved/renamed dir, or a default_skills entry the manifest
forgot — without booting the SDK or the ``claude`` CLI. The skill's *invocation* name is
its SKILL.md frontmatter ``name``, which is what the SDK ``skills=`` filter matches, so
that is what these assert against.
"""

import json
from pathlib import Path

from chief.config import Settings

_REPO = Path(__file__).resolve().parents[1]
_PLUGIN = _REPO / "vendor" / "chief-skills"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"


def _skill_name(skill_md: Path) -> str:
    """The ``name:`` from a SKILL.md's leading YAML frontmatter block."""
    lines = skill_md.read_text().splitlines()
    assert lines[0] == "---", f"{skill_md} has no frontmatter fence"
    end = lines.index("---", 1)
    for line in lines[1:end]:
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no name: in {skill_md} frontmatter")


def _manifest_skills() -> list[str]:
    manifest: dict[str, list[str]] = json.loads(_MANIFEST.read_text())
    return manifest["skills"]


def test_every_default_skill_resolves_to_a_skill_md_on_disk() -> None:
    by_name: dict[str, Path] = {}
    for rel in _manifest_skills():
        skill_md = _PLUGIN / rel / "SKILL.md"
        assert skill_md.is_file(), f"manifest entry {rel!r} has no SKILL.md"
        by_name[_skill_name(skill_md)] = skill_md

    for name in Settings.model_fields["default_skills"].default:
        assert name in by_name, f"default skill {name!r} not exposed by the manifest"


def test_setup_morning_brief_drives_the_recurring_schedule_tool() -> None:
    skill_md = _PLUGIN / "skills" / "setup-morning-brief" / "SKILL.md"
    assert _skill_name(skill_md) == "setup-morning-brief"
    body = skill_md.read_text()
    # The skill's whole job: register a recurring *wakeup* via the M9 schedule tool.
    assert "mcp__chief_schedule__schedule_recurring" in body
    assert "wakeup" in body
