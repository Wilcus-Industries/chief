"""The chief-memory CLI: search and reindex emit note-path-carrying lines."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from chief_obsidian_memory import cli, config, embedding
from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.index import VaultIndex


@pytest.fixture
def warm_cache(embedder: Any) -> Iterator[None]:
    """Seed the module model cache so the CLI (which builds its own index with
    no injected model) reuses the session-loaded model instead of reloading."""
    embedding._MODEL_CACHE["minishlab/potion-base-8M"] = embedder
    yield
    embedding._MODEL_CACHE.clear()


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


def _write_config(tmp_path: Path, vault: Path, **extra: str) -> None:
    lines = ["obsidian_memory:", "  vault_paths:", f"    - {vault}"]
    lines += [f"  {k}: {v}" for k, v in extra.items()]
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")


def test_search_resolves_vault_from_the_daemon_config_without_flags(
    vault: Path,
    tmp_path: Path,
    warm_cache: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # SKILL.md runs `chief-memory search "<query>"` bare; with an
    # obsidian_memory block in config.yaml the CLI resolves the vault from
    # config — no --vault and no CHIEF_MEMORY_VAULT set.
    _write_config(tmp_path, vault)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHIEF_MEMORY_VAULT", raising=False)
    monkeypatch.delenv("CHIEF_MEMORY_INDEX_HOME", raising=False)

    assert cli.main(["reindex"]) == 0
    assert "reindexed 5 chunks" in capsys.readouterr().out

    assert cli.main(["search", "leaking roof in the rain"]) == 0
    assert "roof.md" in capsys.readouterr().out


def test_no_vault_in_config_or_flags_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.yaml").write_text("obsidian_memory: {}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHIEF_MEMORY_VAULT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["search", "anything"])
    assert "no vault" in str(excinfo.value)


def test_cli_and_hook_derive_the_same_collection_and_index_home(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # reindex must write the collection the hook queries: for one config the
    # CLI and the hook derive the same index_home and collection name, so a
    # tuned embed_model/scope can't silently split them into two collections.
    embed_model = "minishlab/potion-base-8M"
    _write_config(tmp_path, vault, embed_model=embed_model)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHIEF_MEMORY_VAULT", raising=False)
    monkeypatch.delenv("CHIEF_MEMORY_INDEX_HOME", raising=False)

    cli_index = cli._index(cli._parser().parse_args(["search", "q"]))

    hook_settings = MemorySettings.from_config(
        {"vault_paths": [str(vault)], "embed_model": embed_model}
    )
    hook_index = VaultIndex(
        vault=vault,
        index_home=config.index_home_for(config.package_data_dir()),
        settings=hook_settings,
    )
    assert cli_index._index_home == hook_index._index_home
    assert cli_index._collection_name() == hook_index._collection_name()
    assert cli_index._settings.embed_model == hook_index._settings.embed_model
