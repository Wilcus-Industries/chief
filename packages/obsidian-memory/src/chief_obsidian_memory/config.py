"""Typed settings for the obsidian-memory package.

Read from the ``obsidian_memory`` config key the manifest declares: the loader
slices that one top-level key and hands its nested mapping to the hook, which
builds a :class:`MemorySettings` — every sub-key optional, code fills defaults.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_DEFAULT_EXCLUDE = (".obsidian/", "templates/", "attachments/")
_TUPLE_KEYS = ("include", "exclude", "vault_paths", "writable_paths")
_PACKAGE_NAME = "obsidian-memory"
_DATA_ROOT = Path("data/hooks")


def package_data_dir() -> Path:
    """The package's on-disk data dir, ``data/hooks/obsidian-memory``.

    Mirrors the hook loader's ``data_root / name`` (``chief.app`` wires
    ``data_root=data/hooks``). The CLI has no ``HookContext``, so it rebuilds
    the same directory from this one place — so it targets the ambient hook's
    index rather than a private one of its own.
    """
    return _DATA_ROOT / _PACKAGE_NAME


def index_home_for(data_dir: Path) -> Path:
    """The index home under a package data dir — the directory holding the
    SQLite store. The single derivation the ambient hook and the CLI share, so
    both open the same store."""
    return data_dir / "index"


@dataclass(frozen=True)
class MemorySettings:
    """How the package indexes and recalls: cadence, scope, and the models.

    ``ambient_n`` — the ambient recall hook fires every Nth owner turn.
    ``window`` — transcript messages the relevance gate sees. ``top_k`` —
    candidates pre-fetched per query, and so also the number of gate calls per
    firing; small on purpose now that recall emits pointers rather than note
    bodies. Four rather than two because retrieval is hybrid — the hook's
    ``ambient_candidates`` reserves half its slots for literal matches, so four
    is the smallest split giving each half more than one candidate.
    ``injection_cap_tokens`` — backstop ceiling on the pointer nudge.
    ``include``/``exclude`` — vault-relative path prefixes gating what indexes.
    ``vault_paths`` — the vault root; only the first entry is used (the code
    is single-vault end-to-end). ``writable_paths`` — where the agent
    may save notes (capability follows config; empty means read-only).

    ``auto_refresh`` — whether every ``search`` first runs the cheap mtime-gated
    sweep that keeps the index current (new/changed/deleted notes), so recall
    never needs a manual reindex; set ``false`` to fall back to manual-reindex
    mode. ``refresh_min_interval_s`` — throttle for that sweep: it is skipped
    when one ran within this many seconds (a per-vault stamp under the index
    home). Defaults to 60s so a burst of searches over a large vault does not
    re-scan on every one; set ``0`` to sweep on literally every search.

    The gate's model is not configured here: it is the ``model:`` field of the
    ``memory-relevance`` classifier definition, which the install seeds to a
    fast OpenRouter model (``openai/gpt-4.1-nano``) rather than the daemon's
    ``default_classifier`` role — a binary gate does not need the agent's big
    model, and that role often routes over a serializing proxy.
    """

    ambient_n: int = 5
    window: int = 10
    top_k: int = 4
    injection_cap_tokens: int = 1500
    embed_model: str = "minishlab/potion-base-8M"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = _DEFAULT_EXCLUDE
    vault_paths: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = ()
    auto_refresh: bool = True
    refresh_min_interval_s: float = 60.0

    @classmethod
    def from_config(cls, mapping: Mapping[str, Any] | None) -> "MemorySettings":
        """Build settings from the sliced ``obsidian_memory`` dict, defaulting
        every missing sub-key and coercing list-valued keys to tuples."""
        raw = dict(mapping or {})
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in known:
                continue
            kwargs[key] = tuple(value) if key in _TUPLE_KEYS else value
        return cls(**kwargs)
