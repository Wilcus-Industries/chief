"""Category-routed subagents + the M10 skills-directory port (issue #87, part of #72).

The real mechanism runs here: a :class:`SubagentSpec` declares a category, and
:func:`build_custom_agents` resolves it through a *real* seeded ``RoutingStore`` (only
sqlite is the in-memory fixture — the routing resolution itself is genuine) into the
model the subagent's ``CustomAgentConfig`` requests. The owner-only gate, the
routing-off / removed-category fallbacks, and the manifest→``skill_directories`` port
are each covered.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.core.routing import RoutingStore
from chief.core.subagents import (
    DEFAULT_SUBAGENTS,
    SubagentSpec,
    build_custom_agents,
    chief_skill_directories,
    load_subagent_specs,
    resolve_subagent_model,
    scaffold_default_subagents,
    skill_directories_for,
)
from chief.memory.versioning import GitVersioner

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")

_REPO = Path(__file__).resolve().parents[1]
_PLUGIN = str(_REPO / "vendor" / "chief-skills")

# research → copilot:auto, code → openrouter:<model>: two categories, two models, so a
# routed subagent set demonstrably picks distinct models from one live table.
_SEED = [
    ("research", "copilot", "auto"),
    ("general", "copilot", "auto"),
    ("code", "openrouter", "deepseek/deepseek-v4-flash"),
]

_RESEARCHER = SubagentSpec(
    name="researcher", description="research", prompt="do research", category="research"
)
_CODER = SubagentSpec(
    name="coder", description="code", prompt="write code", category="code"
)


async def _seeded(sf: async_sessionmaker[AsyncSession]) -> RoutingStore:
    store = RoutingStore(sf, default_category="general")
    await store.seed(_SEED)
    return store


async def test_declared_category_resolves_to_its_routed_model(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # AC1 mechanism: each subagent runs on the model ITS category resolves to — a
    # distinct model per category, straight from the live routing table.
    routing = await _seeded(session_factory)

    agents = build_custom_agents(
        [_RESEARCHER, _CODER], routing=routing, tier="owner"
    )

    by_name = {a["name"]: a for a in agents}
    assert by_name["researcher"]["model"] == "auto"
    assert by_name["coder"]["model"] == "deepseek/deepseek-v4-flash"
    # The declared prompt/description ride through unchanged (identity, not model).
    assert by_name["researcher"]["prompt"] == "do research"


async def test_guests_get_no_subagents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # AC2: the gate is in build_custom_agents itself — any non-owner tier yields [], so
    # a guest session can never carry a subagent even if the guest path is mis-wired to
    # call this. Not an assumption that guests "never reach here".
    routing = await _seeded(session_factory)

    assert build_custom_agents(DEFAULT_SUBAGENTS, routing=routing, tier="guest") == []
    assert build_custom_agents(DEFAULT_SUBAGENTS, routing=routing, tier="casual") == []


async def test_routing_off_omits_model_so_subagent_uses_parent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Routing disabled (no table wired): a category can't be resolved, so the per-agent
    # model is omitted and the SDK falls back to the parent session's model.
    agents = build_custom_agents([_RESEARCHER], routing=None, tier="owner")

    assert agents[0]["name"] == "researcher"
    assert "model" not in agents[0]


async def test_removed_category_falls_back_to_default_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #83 renames/removes categories at runtime. A subagent whose category no longer has
    # a row still resolves — RoutingStore.resolve falls back to the default category's
    # target — so the subagent keeps working rather than crashing or dropping out.
    routing = await _seeded(session_factory)
    ghost = SubagentSpec(
        name="ghost", description="x", prompt="x", category="was-removed"
    )

    agents = build_custom_agents([ghost], routing=routing, tier="owner")

    # Default category "general" → auto, so the removed-category subagent inherits it.
    assert agents[0]["model"] == "auto"


async def test_unseeded_table_omits_model(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An empty table resolves nothing (not even the default) → model omitted, parent
    # fallback. The one case RoutingStore.resolve returns None.
    routing = RoutingStore(session_factory)
    await routing.load()  # nothing seeded

    agents = build_custom_agents([_RESEARCHER], routing=routing, tier="owner")

    assert "model" not in agents[0]


async def test_declared_skills_ride_through(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A spec that names skills preloads them into the subagent (the SDK's per-agent
    # skills field); the shipped defaults set none, so this is the opt-in path.
    routing = await _seeded(session_factory)
    with_skills = SubagentSpec(
        name="doc", description="d", prompt="p", category="general",
        skills=("docx", "pdf"),
    )

    agents = build_custom_agents([with_skills], routing=routing, tier="owner")

    assert agents[0]["skills"] == ["docx", "pdf"]


async def test_default_subagents_declare_known_categories(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The shipped defaults must declare categories the default routing set knows, or
    # they would silently fall back to the default target instead of the intended model.
    from chief.core.routing import DEFAULT_CATEGORIES

    for spec in DEFAULT_SUBAGENTS:
        assert spec.category in DEFAULT_CATEGORIES
        assert not spec.skills  # defaults stay decoupled from skills_enabled


async def test_resolve_subagent_model_none_when_routing_off() -> None:
    assert resolve_subagent_model("research", None) is None


# --- M10 skills → skill_directories port -------------------------------------------


def test_skill_directories_are_absolute_dirs_for_curated_skills() -> None:
    # The curated names map to their absolute on-disk skill directories (submodules
    # initialised in this worktree, so the upstream dirs exist).
    dirs = skill_directories_for(_PLUGIN, ("docx", "setup-morning-brief"))

    assert len(dirs) == 2
    for path in dirs:
        assert Path(path).is_absolute()
        assert (Path(path) / "SKILL.md").is_file()
    assert any(path.endswith("/upstream/skills/docx") for path in dirs)
    assert any(path.endswith("/skills/setup-morning-brief") for path in dirs)


def test_skill_directories_excludes_non_curated() -> None:
    # Scoping parity with claude's skills= filter: a skill not in the enable-list is not
    # handed to the session, even though its dir is in the manifest.
    dirs = skill_directories_for(_PLUGIN, ("docx",))

    assert len(dirs) == 1
    assert not any("pptx" in path for path in dirs)


def test_skill_directories_empty_when_nothing_curated() -> None:
    assert skill_directories_for(_PLUGIN, ()) == []


def test_skill_directories_are_leaf_dirs_never_parent_roots() -> None:
    # Verified against the live Copilot runtime 1.0.67 (#98): it resolves each
    # skill_directories entry by scanning it *recursively* for SKILL.md. Handing it
    # vendor/chief-skills/upstream/ yields all 18 upstream skills; handing it a dir
    # whose SKILL.md sits directly inside yields exactly that one. Curation rests on
    # every entry being a leaf — one SKILL.md in its whole subtree, its own. A dir
    # with a nested SKILL.md below it would silently re-expose an uncurated skill.
    from chief.config import Settings

    curated = Settings.model_fields["default_skills"].default
    dirs = skill_directories_for(_PLUGIN, curated)

    assert len(dirs) == len(curated)
    for path in dirs:
        found = list(Path(path).rglob("SKILL.md"))
        assert found == [Path(path) / "SKILL.md"], f"{path} is a parent root"

    # Positive control for the scan semantics the guard defends against: the upstream
    # root really does hold many SKILL.md, so passing it would leak the uncurated set.
    upstream_root = Path(_PLUGIN) / "upstream"
    assert len(list(upstream_root.rglob("SKILL.md"))) > len(curated)


# --- on-disk subagent loading (#105, part of #103) ----------------------------------


def _write_md(path: Path, frontmatter: str, body: str) -> None:
    path.write_text(f"---\n{frontmatter}\n---\n{body}")


async def test_load_subagent_specs_reads_category_and_resolves_model(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # AC1: a chief-authored .md becomes a SubagentSpec, and that spec's category rides
    # through build_custom_agents to the model the LIVE routing table resolves it to.
    _write_md(
        tmp_path / "researcher.md",
        "category: research\ndescription: Focused background research.",
        "You are chief's research subagent. Investigate thoroughly.",
    )

    specs = load_subagent_specs(tmp_path)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "researcher"
    assert spec.category == "research"
    assert spec.description == "Focused background research."
    assert spec.prompt == "You are chief's research subagent. Investigate thoroughly."

    routing = await _seeded(session_factory)
    agents = build_custom_agents(specs, routing=routing, tier="owner")
    assert agents[0]["model"] == routing.resolve("research").model  # type: ignore[union-attr]


async def test_load_subagent_specs_skills_ride_through(tmp_path: Path) -> None:
    # AC6: an optional skills: YAML list reaches SubagentSpec.skills and, via
    # build_custom_agents, the built CustomAgentConfig["skills"].
    _write_md(
        tmp_path / "doc.md",
        "category: general\ndescription: doc helper\nskills:\n  - docx\n  - pdf",
        "Help with documents.",
    )

    specs = load_subagent_specs(tmp_path)

    assert specs[0].skills == ("docx", "pdf")
    agents = build_custom_agents(specs, routing=None, tier="owner")
    assert agents[0]["skills"] == ["docx", "pdf"]


async def test_load_subagent_specs_ignores_model_in_frontmatter(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # AC4: a file that pins model: must not influence CustomAgentConfig["model"] — the
    # category resolution is the only model source. With routing off, model is absent
    # entirely even though the file declared one.
    _write_md(
        tmp_path / "pinned.md",
        "category: research\ndescription: d\nmodel: claude-opus-4-8",
        "prompt body",
    )

    specs = load_subagent_specs(tmp_path)

    assert specs[0].category == "research"  # only the category rides through
    agents = build_custom_agents(specs, routing=None, tier="owner")
    assert "model" not in agents[0]


async def test_load_subagent_specs_skips_malformed_file_with_warning(
    tmp_path: Path, caplog: Any
) -> None:
    # AC5: a malformed file (no frontmatter fences) is skipped with a logged warning;
    # the valid sibling file still loads.
    (tmp_path / "broken.md").write_text("not frontmatter at all\njust text")
    _write_md(
        tmp_path / "ok.md", "category: general\ndescription: fine", "a prompt"
    )

    with caplog.at_level(logging.WARNING, logger="chief.core.subagents"):
        specs = load_subagent_specs(tmp_path)

    assert [s.name for s in specs] == ["ok"]
    assert any("broken.md" in record.message for record in caplog.records)


async def test_load_subagent_specs_skips_unreadable_directory_entry(
    tmp_path: Path, caplog: Any
) -> None:
    # #105 AC5: a *.md path that isn't a regular file (here, a directory) must degrade
    # to a skipped entry with a logged warning, not raise OSError out of read_text().
    (tmp_path / "notafile.md").mkdir()
    _write_md(tmp_path / "ok.md", "category: general\ndescription: fine", "a prompt")

    with caplog.at_level(logging.WARNING, logger="chief.core.subagents"):
        specs = load_subagent_specs(tmp_path)

    assert [s.name for s in specs] == ["ok"]
    assert any("notafile.md" in record.message for record in caplog.records)


async def test_load_subagent_specs_skips_non_utf8_file(
    tmp_path: Path, caplog: Any
) -> None:
    # #115: read_text() raises UnicodeDecodeError on non-UTF8 bytes, and that is a
    # ValueError -- NOT an OSError -- so the OSError-only guard let it abort the
    # session build. A non-UTF8 *.md must skip with a warning like any malformed file.
    (tmp_path / "latin1.md").write_bytes(b"---\ncategory: caf\xe9\n---\nprompt")
    _write_md(tmp_path / "ok.md", "category: general\ndescription: fine", "a prompt")

    with caplog.at_level(logging.WARNING, logger="chief.core.subagents"):
        specs = load_subagent_specs(tmp_path)

    assert [s.name for s in specs] == ["ok"]
    assert any("latin1.md" in record.message for record in caplog.records)


async def test_load_subagent_specs_missing_category_is_malformed(
    tmp_path: Path,
) -> None:
    _write_md(tmp_path / "nocat.md", "description: only description", "prompt")

    assert load_subagent_specs(tmp_path) == ()


def test_load_subagent_specs_absent_dir_returns_empty(tmp_path: Path) -> None:
    # AC8 (loader half): a directory that doesn't exist yields () so the caller keeps
    # DEFAULT_SUBAGENTS.
    assert load_subagent_specs(tmp_path / "does-not-exist") == ()


def test_load_subagent_specs_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert load_subagent_specs(tmp_path) == ()


# --- chief-authored skills root scanner (#106, part of #103) ------------------------


def _write_skill_md(path: Path, *, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\nDo the thing.")


def test_chief_skill_directories_finds_flat_skill(tmp_path: Path) -> None:
    # AC1: a flat foo/SKILL.md contributes the absolute directory of foo.
    _write_skill_md(tmp_path / "foo" / "SKILL.md", name="foo")

    dirs = chief_skill_directories(tmp_path)

    assert dirs == [str((tmp_path / "foo").resolve())]


def test_chief_skill_directories_nested_skill_excludes_parent_root(
    tmp_path: Path,
) -> None:
    # AC2/AC3: foo/bar/SKILL.md with no SKILL.md directly in foo/ contributes foo/bar
    # only — never the ancestor foo/ or the scanned root itself.
    _write_skill_md(tmp_path / "foo" / "bar" / "SKILL.md", name="bar")

    dirs = chief_skill_directories(tmp_path)

    assert dirs == [str((tmp_path / "foo" / "bar").resolve())]
    assert str((tmp_path / "foo").resolve()) not in dirs
    assert str(tmp_path.resolve()) not in dirs


def test_chief_skill_directories_logs_the_parent_root_it_skips(
    tmp_path: Path, caplog: Any
) -> None:
    # #117: a valid foo/SKILL.md holding a nested bar/SKILL.md is dropped by the
    # leaf-check. Silently, before this: a vanished skill was undiagnosable. The log
    # must name both the skipped dir and the nested path that triggered the guard.
    _write_skill_md(tmp_path / "foo" / "SKILL.md", name="foo")
    _write_skill_md(tmp_path / "foo" / "bar" / "SKILL.md", name="bar")

    with caplog.at_level(logging.DEBUG, logger="chief.core.subagents"):
        dirs = chief_skill_directories(tmp_path)

    # Guard behavior is unchanged: the leaf wins, the parent root is never returned.
    assert dirs == [str((tmp_path / "foo" / "bar").resolve())]
    message = " ".join(record.getMessage() for record in caplog.records)
    assert str(tmp_path / "foo") in message
    assert str(tmp_path / "foo" / "bar" / "SKILL.md") in message


def test_chief_skill_directories_skips_malformed_alongside_valid(
    tmp_path: Path, caplog: Any
) -> None:
    # AC4: a SKILL.md with no frontmatter fence is skipped with a logged warning; the
    # valid sibling skill still loads.
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("not frontmatter at all")
    _write_skill_md(tmp_path / "ok" / "SKILL.md", name="ok")

    with caplog.at_level(logging.WARNING, logger="chief.core.subagents"):
        dirs = chief_skill_directories(tmp_path)

    assert dirs == [str((tmp_path / "ok").resolve())]
    assert any("broken" in record.message for record in caplog.records)


def test_chief_skill_directories_absent_dir_returns_empty(tmp_path: Path) -> None:
    # AC6: an absent directory yields [] so the caller adds nothing extra.
    assert chief_skill_directories(tmp_path / "does-not-exist") == []


# --- scaffolding DEFAULT_SUBAGENTS to disk (#108, part of #103) ---------------------


def test_scaffold_writes_default_subagents_to_empty_dir(tmp_path: Path) -> None:
    # AC1: an empty dir gets both built-ins seeded as .md files.
    scaffold_default_subagents(tmp_path)

    assert (tmp_path / "researcher.md").is_file()
    assert (tmp_path / "coder.md").is_file()


def test_scaffold_creates_absent_directory(tmp_path: Path) -> None:
    # AC1: an absent directory is created and seeded.
    target = tmp_path / "subagents"

    scaffold_default_subagents(target)

    assert target.is_dir()
    assert (target / "researcher.md").is_file()
    assert (target / "coder.md").is_file()


def test_scaffolded_files_round_trip_to_defaults(tmp_path: Path) -> None:
    # AC2: scaffolded files round-trip through load_subagent_specs back to
    # DEFAULT_SUBAGENTS. Set comparison, not tuple/list: load_subagent_specs sorts
    # alphabetically (coder, researcher) while DEFAULT_SUBAGENTS is declaration order
    # (researcher, coder); SubagentSpec is frozen/hashable so set equality is exact.
    scaffold_default_subagents(tmp_path)

    assert set(load_subagent_specs(tmp_path)) == set(DEFAULT_SUBAGENTS)


def test_scaffold_skips_when_dir_non_empty(tmp_path: Path) -> None:
    # AC4 mechanism: a non-empty dir (even with only one file) is never re-seeded.
    _write_md(
        tmp_path / "researcher.md",
        "category: research\ndescription: mine",
        "my own researcher prompt",
    )

    scaffold_default_subagents(tmp_path)

    assert not (tmp_path / "coder.md").exists()


def test_scaffold_never_overwrites_edited_file(tmp_path: Path) -> None:
    # AC3 mechanism: an edited scaffolded file survives verbatim.
    edited = "---\ncategory: code\ndescription: edited\n---\nedited prompt\n"
    (tmp_path / "researcher.md").write_text(edited)

    scaffold_default_subagents(tmp_path)

    assert (tmp_path / "researcher.md").read_text() == edited


def test_scaffold_reseeds_emptied_dir(tmp_path: Path) -> None:
    # AC5: emptying the directory entirely re-seeds both files on the next boot.
    scaffold_default_subagents(tmp_path)
    (tmp_path / "researcher.md").unlink()
    (tmp_path / "coder.md").unlink()

    scaffold_default_subagents(tmp_path)

    assert (tmp_path / "researcher.md").is_file()
    assert (tmp_path / "coder.md").is_file()


def test_scaffold_degrades_when_dir_is_a_non_directory_file(
    tmp_path: Path, caplog: Any
) -> None:
    # #123: subagents_dir pointing at an existing file made the unguarded mkdir raise
    # FileExistsError, aborting every owner session build. It must log and return,
    # degrading like the rest of the module.
    target = tmp_path / "subagents"
    target.write_text("not a directory")

    with caplog.at_level(logging.WARNING, logger="chief.core.subagents"):
        scaffold_default_subagents(target)

    assert target.read_text() == "not a directory"  # never clobbered
    assert any("subagents" in record.message for record in caplog.records)


def test_scaffold_is_idempotent_across_repeated_and_concurrent_builds(
    tmp_path: Path,
) -> None:
    # #124: the emptiness scan runs on every owner spawn and its check-then-write is a
    # TOCTOU. It stays benign because the seed content is fixed and each write is
    # exists-guarded, so racing builds converge on identical bytes and none raise.
    async def _race() -> None:
        await asyncio.gather(
            *(asyncio.to_thread(scaffold_default_subagents, tmp_path) for _ in range(8))
        )

    asyncio.run(_race())
    first = {p.name: p.read_text() for p in sorted(tmp_path.glob("*.md"))}

    for _ in range(3):
        scaffold_default_subagents(tmp_path)

    assert {p.name: p.read_text() for p in sorted(tmp_path.glob("*.md"))} == first
    assert set(load_subagent_specs(tmp_path)) == set(DEFAULT_SUBAGENTS)


@requires_git
async def test_git_revert_of_subagent_reflected_in_next_load(tmp_path: Path) -> None:
    # #110: a git revert of a committed subagent file is an owner action outside
    # chief's own turn loop, so the next fresh scan must reflect it — proving the
    # spawn path (load_subagent_specs re-reads the dir every time) sees a revert,
    # not a stale in-memory view.
    harness = tmp_path / "harness"
    subagents = harness / "subagents"
    subagents.mkdir(parents=True)
    versioner = GitVersioner(
        harness, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()

    _write_md(
        subagents / "foo.md",
        "category: general\ndescription: a temp subagent",
        "temporary prompt",
    )
    await versioner.commit("chief: harness auto-save")
    assert {s.name for s in load_subagent_specs(subagents)} == {"foo"}

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(harness), "revert", "--no-edit", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()

    assert "foo" not in {s.name for s in load_subagent_specs(subagents)}
