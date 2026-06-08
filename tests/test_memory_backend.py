"""MarkdownMemory: write/overwrite, index lines, purge, forget, list, scaffold."""

from pathlib import Path

from chief.memory.markdown_backend import MarkdownMemory
from chief.memory.store import OWNER_NAMESPACE, Fact
from chief.memory.versioning import NullVersioner


def _memory(tmp_path: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_path, versioner=NullVersioner(), owner_name="Will")


async def _write(memory: MarkdownMemory, **kw: str | None) -> Fact:
    base: dict[str, str | None] = dict(
        namespace=OWNER_NAMESPACE,
        slug="mornings",
        title="Prefers mornings",
        body="Books calls before noon.",
        provenance="inferred",
        trust="high",
    )
    base.update(kw)
    return await memory.write_fact(**base)  # type: ignore[arg-type]


async def test_write_fact_creates_file_with_frontmatter(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()

    fact = await _write(memory)

    path = tmp_path / "facts" / "owner" / "mornings.md"
    assert path.exists()
    text = path.read_text()
    assert "trust: high" in text
    assert "provenance: inferred" in text
    assert "Books calls before noon." in text
    assert fact.created  # stamped on write
    # The auto-generated listing now references the fact file.
    assert "facts/owner/mornings.md" in memory.facts_listing()
    assert "Prefers mornings" in memory.facts_listing()


async def test_write_fact_overwrites_on_slug_collision(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()

    await _write(memory, body="Books calls before noon.")
    await _write(memory, title="Mornings only", body="Actually only 9-11am.")

    facts = memory.list_facts(OWNER_NAMESPACE)
    assert len(facts) == 1  # overwritten, not accumulated
    assert facts[0].body == "Actually only 9-11am."
    # Exactly one line for the slug in the auto-generated listing.
    listing = memory.facts_listing()
    assert listing.count("facts/owner/mornings.md") == 1
    assert "Mornings only" in listing


async def test_purge_expired_drops_ttl_facts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    await memory.write_fact(
        namespace=OWNER_NAMESPACE, slug="vacation", title="On vacation",
        body="Back Monday.", provenance="owner-stated", trust="high",
        expires="2000-01-01T00:00:00+00:00",
    )
    await _write(memory)  # a non-expiring fact survives

    removed = await memory.purge_expired()

    assert removed == 1
    slugs = {f.slug for f in memory.list_facts(OWNER_NAMESPACE)}
    assert slugs == {"mornings"}
    assert "facts/owner/vacation.md" not in memory.facts_listing()
    assert not (tmp_path / "facts" / "owner" / "vacation.md").exists()


async def test_purge_keeps_unexpired_future_ttl(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    await memory.write_fact(
        namespace=OWNER_NAMESPACE, slug="vacation", title="On vacation",
        body="Back later.", provenance="owner-stated", trust="high",
        expires="2999-01-01T00:00:00+00:00",
    )

    assert await memory.purge_expired() == 0
    assert {f.slug for f in memory.list_facts(OWNER_NAMESPACE)} == {"vacation"}


async def test_forget_removes_file_and_index_line(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    await _write(memory)

    removed = await memory.forget(OWNER_NAMESPACE, "morning")

    assert [f.slug for f in removed] == ["mornings"]
    assert memory.list_facts(OWNER_NAMESPACE) == []
    assert "facts/owner/mornings.md" not in memory.facts_listing()
    assert not (tmp_path / "facts" / "owner" / "mornings.md").exists()


async def test_forget_no_match_returns_empty(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    await _write(memory)

    assert await memory.forget(OWNER_NAMESPACE, "nonsense") == []
    assert len(memory.list_facts(OWNER_NAMESPACE)) == 1


async def test_ensure_scaffold_seeds_starter_files(tmp_path: Path) -> None:
    memory = _memory(tmp_path)

    await memory.ensure_scaffold()

    assert (tmp_path / "Soul.md").exists()
    assert (tmp_path / "User.md").exists()
    assert not (tmp_path / "MEMORY.md").exists()  # no longer created
    assert (tmp_path / "facts" / "owner").is_dir()
    assert "Will" in memory.user()  # seeded from owner_name


async def test_ensure_scaffold_preserves_hand_edits(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    (tmp_path / "Soul.md").write_text("# my own soul\n")

    await memory.ensure_scaffold()  # second boot must not clobber the edit

    assert memory.soul() == "# my own soul\n"


def test_readers_return_empty_before_scaffold(tmp_path: Path) -> None:
    memory = _memory(tmp_path)

    assert memory.facts_listing() == ""
    assert memory.soul() == ""
    assert memory.user() == ""
    assert memory.list_facts(OWNER_NAMESPACE) == []


async def test_scaffold_user_md_has_preferences_and_facts_sections(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)

    await memory.ensure_scaffold()

    user_text = memory.user()
    assert "## Preferences" in user_text
    assert "## Facts" in user_text


async def test_scaffold_user_md_heading_uses_owner_name(tmp_path: Path) -> None:
    memory = MarkdownMemory(
        tmp_path, versioner=NullVersioner(), owner_name="William Chastain"
    )

    await memory.ensure_scaffold()

    assert "# William Chastain" in memory.user()
