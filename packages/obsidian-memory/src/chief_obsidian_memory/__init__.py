"""Obsidian-vault memory for chief.

An installable subpackage the ``obsidian-memory`` hook package depends on. It
indexes an Obsidian vault (heading-chunked, model2vec-embedded) into one SQLite
store outside the vault holding both retrieval halves — FTS5 for literal text,
sqlite-vec for meaning — answers hybrid + wikilink-graph queries, and drives the
ambient-recall pre_turn hook. Heavy dependencies (sqlite-vec, model2vec,
obsidiantools, networkx) are imported lazily inside the functions that need
them, never at package import, so boot stays light.
"""
