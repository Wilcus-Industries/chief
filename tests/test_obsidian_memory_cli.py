"""The chief-memory CLI: search and reindex emit note-path-carrying lines."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from chief_obsidian_memory import cli, index


@pytest.fixture
def warm_cache(embedder: Any) -> Iterator[None]:
    """Seed the module model cache so the CLI (which builds its own index with
    no injected model) reuses the session-loaded model instead of reloading."""
    index._MODEL_CACHE["minishlab/potion-base-8M"] = embedder
    yield
    index._MODEL_CACHE.clear()


def _args(vault: Path, tmp_path: Path, *rest: str) -> list[str]:
    return [*rest, "--vault", str(vault), "--index-home", str(tmp_path / "idx")]


def test_reindex_then_search_prints_note_paths(
    vault: Path, tmp_path: Path, warm_cache: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(_args(vault, tmp_path, "reindex")) == 0
    out = capsys.readouterr().out
    assert "reindexed 5 chunks" in out

    assert cli.main(_args(vault, tmp_path, "search", "leaking roof in the rain")) == 0
    out = capsys.readouterr().out
    # Every result line names its source note, so recall is traceable.
    assert "roof.md" in out


def test_reindex_picks_up_an_out_of_band_change(
    vault: Path, tmp_path: Path, warm_cache: None, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(_args(vault, tmp_path, "reindex"))
    capsys.readouterr()
    (vault / "tomatoes.md").write_text(
        "# Blight\n\nEarly blight fungus spots the lower tomato leaves in "
        "humid weather.\n"
    )
    cli.main(_args(vault, tmp_path, "reindex"))
    capsys.readouterr()

    cli.main(_args(vault, tmp_path, "search", "fungal disease spotting the leaves"))
    out = capsys.readouterr().out
    assert "tomatoes.md :: Blight" in out


def test_unknown_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bogus"])
    assert excinfo.value.code != 0
