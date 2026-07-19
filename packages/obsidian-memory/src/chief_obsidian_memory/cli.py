"""The ``chief-memory`` command line: search and reindex an Obsidian vault.

Vault and index home resolve from flags first, then the ``CHIEF_MEMORY_VAULT``
and ``CHIEF_MEMORY_INDEX_HOME`` environment variables the installer sets, then
the built-in default index home under chief's data dir. Every result line
carries the source note path so recall is always traceable to a note.
"""

import argparse
import os
from pathlib import Path

from chief_obsidian_memory.config import MemorySettings
from chief_obsidian_memory.graph import VaultGraph, related
from chief_obsidian_memory.index import VaultIndex

DEFAULT_INDEX_HOME = "data/hooks/obsidian-memory/index"


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
    count = _index(args).build()
    print(f"reindexed {count} chunks from {_vault_path(args)}")
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
    vault = _vault_path(args)
    return VaultGraph(vault=vault, settings=MemorySettings(vault_paths=(str(vault),)))


def _index(args: argparse.Namespace) -> VaultIndex:
    vault = _vault_path(args)
    index_home = Path(
        args.index_home
        or os.environ.get("CHIEF_MEMORY_INDEX_HOME")
        or DEFAULT_INDEX_HOME
    )
    settings = MemorySettings(vault_paths=(str(vault),))
    return VaultIndex(vault=vault, index_home=index_home, settings=settings)


def _vault_path(args: argparse.Namespace) -> Path:
    vault = args.vault or os.environ.get("CHIEF_MEMORY_VAULT")
    if not vault:
        raise SystemExit(
            "no vault: pass --vault or set CHIEF_MEMORY_VAULT"
        )
    return Path(vault)


if __name__ == "__main__":
    raise SystemExit(main())
