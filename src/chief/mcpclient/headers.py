"""``${secret:name}`` expansion for an MCP server's static HTTP headers.

A header value names a secret instead of carrying it, so ``config.yaml`` never
holds one. The value is read where every other core secret resolves: the
upper-cased env var first, then ``secrets/<name>``. Only that literal form is
expanded — no other ``${...}`` is interpolated, and a name can never be a path.
"""

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from chief.config.coerce import read_secret

_REF = re.compile(r"\$\{secret:([^}]*)\}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def expand_headers(
    raw: Mapping[str, str], read: Callable[[Path], str] = read_secret
) -> dict[str, str]:
    """Return ``raw`` with every ``${secret:name}`` replaced by its value.

    Raises ``ValueError`` naming the header and the secret (never a value) when
    a reference is malformed, resolves to nothing, or names a secret that cannot
    be read, so the caller can skip the one server instead of sending a literal
    placeholder to it.
    """
    return {
        header: _expand(header, value, read) for header, value in raw.items()
    }


def _expand(header: str, value: str, read: Callable[[Path], str]) -> str:
    return _REF.sub(lambda ref: _resolve(header, ref[1], read), value)


def _resolve(header: str, name: str, read: Callable[[Path], str]) -> str:
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(
            f"header {header!r}: {name!r} is not a valid secret name "
            "(letters, digits, '-' and '_' only)"
        )
    try:
        value = os.environ.get(name.upper()) or read(Path("secrets") / name)
    except OSError as exc:  # a directory, or a file this user may not read
        raise ValueError(
            f"header {header!r}: secret {name!r} could not be read "
            f"({type(exc).__name__})"
        ) from exc
    if not value:
        raise ValueError(
            f"header {header!r}: secret {name!r} is not set "
            f"(env {name.upper()} or secrets/{name})"
        )
    return value
