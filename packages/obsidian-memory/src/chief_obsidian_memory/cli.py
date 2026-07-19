"""The ``chief-memory`` command line: search and reindex an Obsidian vault.

Settings resolve from the daemon's ``obsidian_memory`` config block — the same
source the ambient hook reads via ``HookContext.config`` — so the CLI and the
hook resolve the identical vault, index home, and embedding model. That keeps
``reindex`` writing the very collection recall queries: a divergent
``embed_model`` or ``include``/``exclude`` would otherwise build a second,
silently-different collection. ``--vault`` / ``CHIEF_MEMORY_VAULT`` and
``--index-home`` / ``CHIEF_MEMORY_INDEX_HOME`` override the config block; every
result line carries its source note path, so recall is always traceable.

Defaults resolve against the **repo root** (the nearest ancestor of the CWD
holding a ``config.yaml``), never the bare CWD: run from the wrong directory,
a CWD-relative CLI silently built a fresh empty index and answered
"no matches" — indistinguishable, to the model, from a broken tool.
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
    settings = _settings(args)
    hits = _index(args).search(args.query, args.k)
    if not hits:
        # Name the resolved paths so an empty answer is checkable, never
        # mistaken for a broken tool.
        print(
            f"no matches (vault={_vault(settings)}, index={_index_home(args)})"
        )
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


def _repo_root() -> Path | None:
    """The nearest ancestor of the CWD that is the daemon repo root — holding
    ``config.yaml`` beside the ``data/installed.yaml`` install registry (this
    CLI only exists once a package install wrote it) — or None when there
    isn't one. Requiring the registry stops a stray config.yaml higher up
    from silently binding recall to the wrong vault and index."""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "config.yaml").is_file() and (
            candidate / "data" / "installed.yaml"
        ).is_file():
            return candidate
    return None


def _settings(args: argparse.Namespace) -> MemorySettings:
    """Resolve settings from the daemon ``obsidian_memory`` config block (found
    via :func:`_repo_root`), then let an explicit ``--vault``/
    ``CHIEF_MEMORY_VAULT`` override its vault path. Fails loudly — naming what
    was searched — when neither config nor flag supplies a vault."""
    from chief.config import load_raw

    root = _repo_root()
    raw = load_raw(root / "config.yaml") if root else {}
    settings = MemorySettings.from_config(raw.get("obsidian_memory"))
    override = args.vault or os.environ.get("CHIEF_MEMORY_VAULT")
    if override:
        settings = replace(settings, vault_paths=(override,))
    if not settings.vault_paths:
        where = (
            f"{root / 'config.yaml'} has no obsidian_memory.vault_paths"
            if root
            else (
                "no chief repo (config.yaml + data/installed.yaml) found "
                f"from {Path.cwd()} upward"
            )
        )
        raise SystemExit(
            f"no vault configured: {where} — pass --vault, set "
            "CHIEF_MEMORY_VAULT, or run from the chief repo"
        )
    return settings


def _index_home(args: argparse.Namespace) -> Path:
    override = args.index_home or os.environ.get("CHIEF_MEMORY_INDEX_HOME")
    if override:
        return Path(override)
    root = _repo_root()
    if root is None:
        raise SystemExit(
            f"no config.yaml found from {Path.cwd()} upward — cannot resolve "
            "the index home; run from the chief repo or pass --index-home"
        )
    return root / index_home_for(package_data_dir())


def _vault(settings: MemorySettings) -> Path:
    return Path(settings.vault_paths[0])


if __name__ == "__main__":
    raise SystemExit(main())
