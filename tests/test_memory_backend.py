"""MarkdownMemory: purge, forget, list, scaffold, readers."""

from pathlib import Path

from chief.memory.markdown_backend import MarkdownMemory
from chief.memory.store import OWNER_NAMESPACE
from chief.memory.versioning import NullVersioner


def _memory(tmp_path: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_path, versioner=NullVersioner(), owner_name="Will")


def _seed_fact(
    tmp_path: Path, slug: str, title: str, expires: str | None = None
) -> None:
    """Write a raw fact file directly, bypassing the removed write_fact API."""
    facts_dir = tmp_path / "facts" / OWNER_NAMESPACE
    facts_dir.mkdir(parents=True, exist_ok=True)
    expires_line = f"expires: {expires}" if expires else "expires: "
    content = (
        f"---\ntitle: {title}\ntrust: high\nprovenance: owner-stated\n"
        f"{expires_line}\ncreated: 2026-01-01T00:00:00+00:00\n---\n"
        "Some body text.\n"
    )
    (facts_dir / f"{slug}.md").write_text(content, encoding="utf-8")


async def test_purge_expired_drops_ttl_facts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    _seed_fact(tmp_path, "vacation", "On vacation", expires="2000-01-01T00:00:00+00:00")
    _seed_fact(tmp_path, "mornings", "Prefers mornings")

    removed = await memory.purge_expired()

    assert removed == 1
    slugs = {f.slug for f in memory.list_facts(OWNER_NAMESPACE)}
    assert slugs == {"mornings"}
    assert "facts/owner/vacation.md" not in memory.facts_listing()
    assert not (tmp_path / "facts" / "owner" / "vacation.md").exists()


async def test_purge_keeps_unexpired_future_ttl(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    _seed_fact(tmp_path, "vacation", "On vacation", expires="2999-01-01T00:00:00+00:00")

    assert await memory.purge_expired() == 0
    assert {f.slug for f in memory.list_facts(OWNER_NAMESPACE)} == {"vacation"}


async def test_forget_removes_file_and_index_line(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    _seed_fact(tmp_path, "mornings", "Prefers mornings")

    removed = await memory.forget(OWNER_NAMESPACE, "morning")

    assert [f.slug for f in removed] == ["mornings"]
    assert memory.list_facts(OWNER_NAMESPACE) == []
    assert "facts/owner/mornings.md" not in memory.facts_listing()
    assert not (tmp_path / "facts" / "owner" / "mornings.md").exists()


async def test_forget_no_match_returns_empty(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    await memory.ensure_scaffold()
    _seed_fact(tmp_path, "mornings", "Prefers mornings")

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
