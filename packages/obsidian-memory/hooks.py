"""Boot-loaded hook shim: re-export the package's register entry point.

The boot loader imports this file by path and calls ``register``. It is kept
deliberately import-light — it pulls in only the subpackage's config-light hook
module, never the vector stack (chromadb, model2vec), which the hook imports
lazily the moment ambient recall actually fires. So boot stays fast even with
the package installed.
"""

from chief_obsidian_memory.hook import register

__all__ = ["register"]
