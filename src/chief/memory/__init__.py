"""Long-term memory (DESIGN: Long-term memory & learning).

Markdown facts with ``[[wikilinks]]``, namespaced by subject. The ``facts/`` listing is
auto-generated at prompt-build time — no stored index file. The
:class:`~chief.memory.store.MemoryStore` protocol keeps the backend swappable (mem0
later); :class:`~chief.memory.markdown_backend.MarkdownMemory` is the M4 implementation.
Writes are versioned through a :class:`~chief.memory.versioning.Versioner` so every
change is reversible.
"""
