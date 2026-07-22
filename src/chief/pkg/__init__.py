"""Package management: the loader/library, the discovery CLI, the clone sync.

The library surface is re-exported here so callers import from `chief.pkg`
rather than reaching into `chief.pkg.loader`.
"""

from chief.pkg.loader import (
    CLONED_PACKAGES_DIR,
    HookSpec,
    McpServerSpec,
    Package,
    PackageLibrary,
    dep_importable,
    validate,
)

__all__ = [
    "CLONED_PACKAGES_DIR",
    "HookSpec",
    "McpServerSpec",
    "Package",
    "PackageLibrary",
    "dep_importable",
    "validate",
]
