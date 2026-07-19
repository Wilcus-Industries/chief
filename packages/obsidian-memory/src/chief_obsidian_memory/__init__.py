"""Obsidian-vault semantic memory for chief.

An installable subpackage the ``obsidian-memory`` hook package depends on. It
indexes an Obsidian vault (heading-chunked, model2vec-embedded into a persistent
chromadb collection outside the vault), answers semantic + wikilink-graph
queries, and drives the ambient-recall pre_turn hook. Heavy dependencies
(chromadb, model2vec, obsidiantools, networkx) are imported lazily inside the
functions that need them, never at package import, so boot stays light.
"""
