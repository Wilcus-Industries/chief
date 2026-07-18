"""The agent's file tools: read/grep (read-only) and write/edit (gated).

``read_file`` and ``grep`` are ``read_only`` so the gate auto-approves them;
both reach the **full filesystem** the OS user can see, not just the repo.
``write_file`` and ``edit_file`` mutate any path the user can write — no repo
confinement and no carve-outs (``secrets``/``.git``/``data``/off-repo all
included). Edits are inert until ``restart`` runs the done-check and reboots;
the approval gate is the only guard on where writes land (guardrails: #197).
"""

import re
from pathlib import Path

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolSpec

GREP_MATCH_CAP = 200

_READ_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read any file the OS user can — your own source, a package manifest, "
        "anything on the machine. Path is absolute or relative to the repo root."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    read_only=True,
)

_GREP_SPEC = ToolSpec(
    name="grep",
    description=(
        "Search files for a regex `pattern` (recursive). Optional `path` sets "
        "the search root (absolute or repo-relative); defaults to the repo. "
        "Returns file:line matches."
    ),
    parameters={
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
    },
    read_only=True,
)

_WRITE_SPEC = ToolSpec(
    name="write_file",
    description=(
        "Write `content` to `path`, replacing it whole and creating parent "
        "dirs. Writes anywhere you have permission. The change is inert until "
        "you `restart` (which runs the done-check and reboots into it)."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
)

_EDIT_SPEC = ToolSpec(
    name="edit_file",
    description=(
        "Replace every occurrence of `old` with `new` in `path` — a targeted "
        "edit that leaves the rest of the file untouched. Errors if `old` is "
        "absent. Inert until you `restart`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    },
)

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


def _grep_tree(root: Path, matcher: re.Pattern[str]) -> list[str]:
    """Walk ``root`` (a file or dir) for lines matching ``matcher``, capped."""
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    hits: list[str] = []
    for path in files:
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary — skip, don't fail the whole search
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                hits.append(f"{path}:{number}:{line}")
                if len(hits) >= GREP_MATCH_CAP:
                    return hits
    return hits


def register_file_tools(registry: ToolRegistry, root: Path) -> None:
    """Expose read_file/grep (read-only) and write_file/edit_file, all rooted at
    ``root`` for relative paths but free to reach absolute paths anywhere."""

    def resolve(path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else root / p

    async def read_file(path: str) -> str:
        target = resolve(path)
        if not target.is_file():
            return f"error: no such file: {path}"
        try:
            return target.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            return f"error: cannot read {path}: {exc}"

    async def grep(pattern: str, path: str | None = None) -> str:
        search = resolve(path) if path is not None else root
        if not search.exists():
            return f"error: no such path: {path}"
        try:
            matcher = re.compile(pattern)
        except re.error as exc:
            return f"error: bad pattern: {exc}"
        hits = _grep_tree(search, matcher)
        return "\n".join(hits) if hits else "no matches"

    async def write_file(path: str, content: str) -> str:
        target = resolve(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        except OSError as exc:
            return f"error: cannot write {path}: {exc}"
        return f"wrote {path} ({len(content)} bytes)"

    async def edit_file(path: str, old: str, new: str) -> str:
        target = resolve(path)
        if not target.is_file():
            return f"error: no such file: {path}"
        try:
            text = target.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            return f"error: cannot read {path}: {exc}"
        count = text.count(old)
        if count == 0:
            return f"error: `old` not found in {path}"
        target.write_text(text.replace(old, new))
        return f"edited {path} ({count} replacement(s))"

    registry.register(Tool(_READ_SPEC, read_file))
    registry.register(Tool(_GREP_SPEC, grep))
    registry.register(Tool(_WRITE_SPEC, write_file))
    registry.register(Tool(_EDIT_SPEC, edit_file))
