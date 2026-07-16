"""Skills: the public SKILL.md convention with progressive disclosure.

A skill is a directory holding a SKILL.md (YAML frontmatter: name,
description; body: the instructions). The system prompt carries only the
one-liners; the agent pulls a skill's full body via the load_skill tool.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        return self.path.read_text()


class SkillLibrary:
    """Scans skill directories and serves names, one-liners, and bodies."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def scan(self) -> list[Skill]:
        skills = []
        for skill_file in sorted(self._root.glob("*/SKILL.md")):
            skill = _parse(skill_file)
            if skill is not None:
                skills.append(skill)
        return skills

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.scan() if s.name == name), None)

    def prompt_lines(self) -> str:
        """The progressive-disclosure index for the system prompt."""
        skills = self.scan()
        if not skills:
            return ""
        lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
        return (
            "\n\nSkills (load the full instructions with the load_skill tool "
            f"when one is relevant):\n{lines}"
        )


def validate(root: Path) -> list[str]:
    """Return the well-formedness problems of every SKILL.md under root.

    Empty means each SKILL.md has YAML frontmatter with a non-empty name
    and description, plus a non-empty body. The self-edit done-check calls
    this, so a bogus write (a placeholder ``...`` body, missing metadata) is
    rolled back rather than merged (issue #186).
    """
    problems: list[str] = []
    for skill_file in sorted(root.glob("*/SKILL.md")):
        problems.extend(_validate_skill(skill_file))
    return problems


def _validate_skill(skill_file: Path) -> list[str]:
    text = skill_file.read_text()
    if not text.startswith("---"):
        return [f"{skill_file}: missing YAML frontmatter"]
    parts = text.split("---", 2)
    if len(parts) < 3:
        return [f"{skill_file}: frontmatter has no closing '---'"]
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        return [f"{skill_file}: frontmatter is not valid yaml ({exc})"]
    if not isinstance(meta, dict):
        return [f"{skill_file}: frontmatter is not a mapping"]
    problems = []
    if not str(meta.get("name") or "").strip():
        problems.append(f"{skill_file}: frontmatter missing 'name'")
    if not str(meta.get("description") or "").strip():
        problems.append(f"{skill_file}: frontmatter missing 'description'")
    if not parts[2].strip():
        problems.append(f"{skill_file}: empty body")
    return problems


def _parse(skill_file: Path) -> Skill | None:
    text = skill_file.read_text()
    if not text.startswith("---"):
        logger.warning("skill %s has no frontmatter; skipping", skill_file)
        return None
    _, frontmatter, _ = text.split("---", 2)
    meta = yaml.safe_load(frontmatter) or {}
    name = str(meta.get("name") or skill_file.parent.name)
    description = str(meta.get("description") or "").strip()
    return Skill(name=name, description=description, path=skill_file)


_LOAD_SPEC = ToolSpec(
    name="load_skill",
    description="Load a skill's full instructions by name.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)


def register_skill_tools(registry: ToolRegistry, library: SkillLibrary) -> None:
    """Expose load_skill backed by the library."""

    async def load_skill(name: str) -> str:
        skill = library.get(name)
        if skill is None:
            return f"error: no skill named '{name}'"
        return skill.body()

    registry.register(Tool(_LOAD_SPEC, load_skill))
