"""The ``chief-memory`` command line: search and reindex an Obsidian vault.

Settings resolve from the daemon's ``obsidian_memory`` config block — the same
source the ambient hook reads via ``HookContext.config`` — so the CLI and the
hook resolve the identical vault, index home, and embedding model. That keeps
``reindex`` writing the very collection recall queries: a divergent
``embed_model`` or ``include``/``exclude`` would otherwise build a second,
silently-different collection. ``--vault`` / ``CHIEF_MEMORY_VAULT`` and
``--index-home`` / ``CHIEF_MEMORY_INDEX_HOME`` override the config block; every
result line carries its source note path, so recall is always traceable.
"""

import argparse
import os
from dataclasses import replace
from pathlib import Path

from chief_obsidian_memory.config import (
    MemorySettings,
    index_home_for,
    package_data_dir,
)
from chief_obsidian_memory.graph import VaultGraph, related
from chief_obsidian_memory.index import VaultIndex


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``chief-memory`` script; returns a process code."""
    parser = _parser()
    args = parser.parse_args(argv)
    exit_code: int = args.run(args)
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chief-memory", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = _common()

    search = sub.add_parser("search", parents=[common], help="search the vault")
    search.add_argument("query")
    search.add_argument("--k", type=int, default=MemorySettings().top_k)
    search.set_defaults(run=_search)

    reindex = sub.add_parser(
        "reindex", parents=[common], help="rebuild the whole index"
    )
    reindex.set_defaults(run=_reindex)

    links = sub.add_parser("links", parents=[common], help="wikilink neighbours")
    links.add_argument("note")
    links.add_argument("--hops", type=int, default=1)
    links.set_defaults(run=_links)

    rel = sub.add_parser(
        "related", parents=[common], help="semantic hits widened by wikilinks"
    )
    rel.add_argument("query")
    rel.add_argument("--k", type=int, default=MemorySettings().top_k)
    rel.add_argument("--hops", type=int, default=1)
    rel.set_defaults(run=_related)
    return parser


def _common() -> argparse.ArgumentParser:
    """Flags shared by every subcommand, so they may follow the subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=None, help="path to the Obsidian vault")
    common.add_argument(
        "--index-home", default=None, help="where the persistent index lives"
    )
    return common


def _search(args: argparse.Namespace) -> int:
    hits = _index(args).search(args.query, args.k)
    if not hits:
        print("no matches")
        return 0
    for hit in hits:
        print(f"{hit.score:.3f}  {hit.note_path} :: {hit.heading}")
    return 0


def _reindex(args: argparse.Namespace) -> int:
    settings = _settings(args)
    count = VaultIndex(
        vault=_vault(settings), index_home=_index_home(args), settings=settings
    ).build()
    print(f"reindexed {count} chunks from {_vault(settings)}")
    return 0


def _links(args: argparse.Namespace) -> int:
    for note_path in _graph(args).neighbors(args.note, args.hops):
        print(note_path)
    return 0


def _related(args: argparse.Namespace) -> int:
    for note_path in related(
        _index(args), _graph(args), args.query, args.k, args.hops
    ):
        print(note_path)
    return 0


def _graph(args: argparse.Namespace) -> VaultGraph:
    settings = _settings(args)
    return VaultGraph(vault=_vault(settings), settings=settings)


def _index(args: argparse.Namespace) -> VaultIndex:
    settings = _settings(args)
    return VaultIndex(
        vault=_vault(settings), index_home=_index_home(args), settings=settings
    )


def _settings(args: argparse.Namespace) -> MemorySettings:
    """Resolve settings from the daemon ``obsidian_memory`` config block, then
    let an explicit ``--vault``/``CHIEF_MEMORY_VAULT`` override its vault path.
    Fails loudly when neither config nor flag supplies a vault."""
    from chief.config import load_raw

    settings = MemorySettings.from_config(load_raw().get("obsidian_memory"))
    override = args.vault or os.environ.get("CHIEF_MEMORY_VAULT")
    if override:
        settings = replace(settings, vault_paths=(override,))
    if not settings.vault_paths:
        raise SystemExit(
            "no vault configured: set obsidian_memory.vault_paths in "
            "config.yaml, pass --vault, or set CHIEF_MEMORY_VAULT"
        )
    return settings


def _index_home(args: argparse.Namespace) -> Path:
    override = args.index_home or os.environ.get("CHIEF_MEMORY_INDEX_HOME")
    return Path(override) if override else index_home_for(package_data_dir())


def _vault(settings: MemorySettings) -> Path:
    return Path(settings.vault_paths[0])


if __name__ == "__main__":
    raise SystemExit(main())
