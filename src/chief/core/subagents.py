"""Category-routed subagents + the M10 skills-directory port (issue #87, part of #72).

Two owner-only capabilities the owner Copilot session hands the SDK, both wired by
:meth:`chief.core.tasks.TaskManager._wire_owner_session`:

* **Category-routed subagents.** A :class:`SubagentSpec` declares a job *category* — not
  a model id. :func:`build_custom_agents` resolves each spec's declared category through
  the **live** routing table (:meth:`chief.core.routing.RoutingStore.resolve`, the same
  resolver ``_resolve_owner_target`` funnels through — never a parallel copy) into the
  model the subagent runs on, and materialises the SDK's ``CustomAgentConfig`` entries.
  The gate is real: :func:`build_custom_agents` hard-refuses any non-owner ``tier`` and
  returns ``[]``, so a guest session can never carry a subagent even if the guest
  path is ever mis-wired to call it (guests get no ``custom_agents`` at all — see
  ``_wire_guest_session``).

* **Skills → ``skill_directories``.** M10 packaged skills reach claude-agent-sdk as a
  plugin manifest + a ``skills=`` name filter; the Copilot SDK instead takes
  ``skill_directories`` (raw directories) with ``enable_skills`` / ``disabled_skills``.
  :func:`skill_directories_for` ports chief's curated set by resolving each manifest
  entry whose SKILL.md name is in the enable-list to its absolute directory. See its
  docstring for the porting gaps (the mapping is not 1:1).

**Model-resolution scope.** A subagent's model is the *direct* routing resolution of its
declared category — the M9/M11 precedence (Opus escalation > budget downgrade > routing)
governs the *task's* own session model, not a subagent's declared category, so it is not
re-applied here. When routing is off, or the category (and the default fallback) has no
row, the model is omitted and the SDK falls back to the parent session's model.

**Provider limitation.** ``CustomAgentConfig`` carries a model *name* but no BYOK
provider; the session's provider is shared by every subagent. A subagent whose category
resolves to an ``openrouter`` target therefore requests that model name under the
session's provider — the runtime falls back to the parent model if it can't serve it
(the same Student-plan ``auto`` constraint the main session already lives under). Noted,
not worked around, at this slice.

**On-disk overrides (#105, part of #103).** :func:`load_subagent_specs` lets a
chief-authored ``.md`` set replace :data:`DEFAULT_SUBAGENTS` — one file per subagent,
loaded fresh at every session spawn (no restart, no approval card) from
``Settings.subagents_dir``. Each file still only *declares* a category; the model is
resolved the same way as the built-ins, through the live routing table.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from copilot.session import CustomAgentConfig

from .routing import RoutingStore

logger = logging.getLogger("chief.core.subagents")

#: The tier that may carry subagents. Owner-only by construction (M10 skills, the
#: owner tool surface) — the guest receptionist never delegates.
OWNER_TIER = "owner"


@dataclass(frozen=True)
class SubagentSpec:
    """A chief subagent definition: identity + a *declared* routing category.

    ``category`` is a routing-table category (one of the seeded ``routes`` rows), not a
    model id — the model is resolved through the table at session build so a category
    renamed or removed at runtime (#83) still resolves via the table's fallback.
    ``skills`` names M10 skills to preload into the subagent's context; the shipped
    defaults set none, so subagents don't couple to ``skills_enabled`` (a subagent that
    declares skills needs ``skill_directories`` wired too — see
    :mod:`chief.core.tasks`).
    """

    name: str
    description: str
    prompt: str
    category: str
    skills: tuple[str, ...] = field(default_factory=tuple)


#: chief's built-in subagents (owner-only, gated by ``subagents_enabled``). Each
#: declares a category from :data:`chief.core.routing.DEFAULT_CATEGORIES`, so with a
#: seeded routing table the researcher and coder resolve to whatever model their
#: category maps to — distinct models from one table, the point of category routing.
DEFAULT_SUBAGENTS: tuple[SubagentSpec, ...] = (
    SubagentSpec(
        name="researcher",
        description="Focused background research: gather, read, and synthesise.",
        prompt=(
            "You are chief's research subagent. Investigate the delegated question "
            "thoroughly, cross-check sources, and return a concise, well-supported "
            "synthesis. State what you could not verify."
        ),
        category="research",
    ),
    SubagentSpec(
        name="coder",
        description="Delegated coding: write, edit, and review code for a sub-task.",
        prompt=(
            "You are chief's coding subagent. Implement the delegated change end to "
            "end, match the surrounding conventions, and report exactly what you "
            "changed and anything left unresolved."
        ),
        category="code",
    ),
)


def resolve_subagent_model(category: str, routing: RoutingStore | None) -> str | None:
    """The model a subagent's declared ``category`` resolves to, or ``None`` to defer.

    ``None`` means "omit the per-agent model, let the SDK use the parent session model":
    either routing is off (``routing is None``) or the table has no row for ``category``
    *nor* the default category (an unseeded table). A category removed/renamed at
    runtime (#83) is not ``None`` — :meth:`RoutingStore.resolve` falls back to the
    default category's target, so a live subagent keeps resolving.
    """
    if routing is None:
        return None
    target = routing.resolve(category)
    return target.model if target is not None else None


def build_custom_agents(
    specs: Sequence[SubagentSpec],
    *,
    routing: RoutingStore | None,
    tier: str,
) -> list[CustomAgentConfig]:
    """Materialise subagent specs into Copilot ``CustomAgentConfig`` entries (#87).

    The owner-only gate is enforced here, not merely by call-site placement: any
    ``tier`` other than :data:`OWNER_TIER` yields ``[]`` — a guest can never carry a
    subagent. Each spec's declared category is resolved through the live ``routing``
    table (:func:`resolve_subagent_model`); the resulting model is set per agent, or
    omitted so the SDK falls back to the parent model (routing off / unseeded table).
    """
    if tier != OWNER_TIER:
        return []
    agents: list[CustomAgentConfig] = []
    for spec in specs:
        agent: CustomAgentConfig = {
            "name": spec.name,
            "description": spec.description,
            "prompt": spec.prompt,
        }
        model = resolve_subagent_model(spec.category, routing)
        if model is not None:
            agent["model"] = model
        if spec.skills:
            agent["skills"] = list(spec.skills)
        agents.append(agent)
    return agents


def _parse_subagent_md(path: Path) -> SubagentSpec | None:
    """Parse one on-disk subagent ``.md`` into a :class:`SubagentSpec`, or ``None``.

    Mirrors the degrade-and-skip shape of :func:`_skill_md_name`: malformed input —
    missing frontmatter fences, unparsable YAML, a missing/wrong-typed required field,
    an unreadable path (a directory, broken symlink, or permission-denied ``.md``,
    #105 AC5), or non-UTF8 bytes (#115) — returns ``None`` rather than raising, so one
    bad file never fails a session build. ``model`` is deliberately never read (#105
    AC4): the model is always the *category*'s live routing resolution, not something
    the file can pin.
    """
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError (#115) — a non-UTF8 .md
        # would otherwise escape this guard and abort the session build.
        return None
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    frontmatter_text = "\n".join(lines[1:end])
    prompt = "\n".join(lines[end + 1 :]).strip()
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    category = data.get("category")
    description = data.get("description")
    if not isinstance(category, str) or not category:
        return None
    if not isinstance(description, str) or not description:
        return None
    skills: tuple[str, ...] = ()
    if "skills" in data:
        raw_skills = data["skills"]
        if not isinstance(raw_skills, list) or not all(
            isinstance(s, str) for s in raw_skills
        ):
            return None
        skills = tuple(raw_skills)
    return SubagentSpec(
        name=path.stem,
        description=description,
        prompt=prompt,
        category=category,
        skills=skills,
    )


def load_subagent_specs(directory: str | Path) -> tuple[SubagentSpec, ...]:
    """Load chief subagents from on-disk ``.md`` files (#105, part of #103).

    One ``.md`` per subagent: filename stem is the name, YAML frontmatter carries
    ``category`` + ``description`` (and optional ``skills``), and the body after the
    closing fence is the prompt — never ``model``, which stays a category resolution,
    not a file-pinned value. A malformed file is skipped with a logged warning, never
    fatal to building a session. ``directory`` absent or holding no loadable ``.md``
    files returns ``()``, so the caller keeps :data:`DEFAULT_SUBAGENTS`.
    """
    path = Path(directory)
    if not path.is_dir():
        return ()
    specs: list[SubagentSpec] = []
    for md_path in sorted(path.glob("*.md")):
        spec = _parse_subagent_md(md_path)
        if spec is None:
            logger.warning("skipping malformed subagent file %s", md_path)
            continue
        specs.append(spec)
    return tuple(specs)


def chief_skill_directories(directory: str | Path) -> list[str]:
    """Absolute leaf directory of every parseable ``SKILL.md`` beneath ``directory``.

    Mirrors :func:`load_subagent_specs`: ``directory`` absent returns ``[]`` (#106).
    Every ``SKILL.md`` found is parsed via :func:`_skill_md_name`; a malformed or
    unreadable one is skipped with a logged warning rather than aborting the scan.

    Per :func:`skill_directories_for`'s verified note, the Copilot runtime scans each
    ``skill_directories`` entry *recursively* for SKILL.md, so a parent root would
    over-expose everything nested beneath it. Each returned dir is therefore checked
    to be a *leaf* — its subtree holds exactly its own SKILL.md — and a dir with a
    nested SKILL.md below it (a parent root) is skipped, never returned.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    directories: list[str] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            name = _skill_md_name(skill_md)
        except (OSError, UnicodeDecodeError):
            name = None
        if name is None:
            logger.warning("skipping malformed chief skill file %s", skill_md)
            continue
        skill_dir = skill_md.parent
        if list(skill_dir.rglob("SKILL.md")) != [skill_dir / "SKILL.md"]:
            continue
        directories.append(str(skill_dir.resolve()))
    return directories


def _skill_md_name(skill_md: Path) -> str | None:
    """The ``name:`` from a SKILL.md's leading YAML frontmatter block, or ``None``.

    Mirrors the manifest-guard parser in ``tests/test_skills_plugin.py`` but degrades to
    ``None`` on a malformed/absent frontmatter rather than raising — a bad skill dir is
    skipped, never fatal to building a session.
    """
    lines = skill_md.read_text().splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    for line in lines[1:end]:
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def skill_directories_for(plugin_path: str, curated: Sequence[str]) -> list[str]:
    """Port chief's curated M10 skill set to Copilot ``skill_directories`` (#87).

    Reads the plugin manifest (``<plugin_path>/.claude-plugin/plugin.json``) and returns
    the **absolute** directory of each listed skill whose SKILL.md ``name`` is in
    ``curated`` — the copilot analogue of claude-agent-sdk's ``skills=`` name filter,
    which chief uses to scope exactly the enable-list.

    PORTING GAPS (the mapping is not 1:1):

    * **No name-filter equivalent.** Copilot has no ``skills=`` allow-filter; scoping is
      done by *which directories* are handed in (here) plus ``enable_skills`` /
      ``disabled_skills``. chief relies on the former.

    **Directory semantics — verified against the live Copilot runtime 1.0.67 (#98).**
    The runtime scans each ``skill_directories`` entry *recursively* for SKILL.md,
    and also discovers a directory whose SKILL.md sits directly inside it. Both hold
    at once, so the per-skill form returned here resolves to exactly the curated set,
    while a parent root (e.g. ``vendor/chief-skills/upstream/``) would resolve to all
    18 upstream skills. Every entry must therefore stay a *leaf* — one SKILL.md in
    its subtree, its own. A dir with a nested SKILL.md below it would silently
    re-expose an uncurated skill;
    ``test_skill_directories_are_leaf_dirs_never_parent_roots`` guards that.
    """
    manifest_path = Path(plugin_path) / ".claude-plugin" / "plugin.json"
    manifest: dict[str, list[str]] = json.loads(manifest_path.read_text())
    wanted = set(curated)
    directories: list[str] = []
    for rel in manifest.get("skills", []):
        skill_dir = (Path(plugin_path) / rel).resolve()
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        if _skill_md_name(skill_md) in wanted:
            directories.append(str(skill_dir))
    return directories
