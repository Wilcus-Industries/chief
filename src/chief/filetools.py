"""read_file + grep: the agent's read-only window into its own repo.

Both tools are ``read_only`` so the gate auto-approves them. Reads are
confined to the repo working tree; ``secrets/`` and ``.git/`` are off-limits
and never appear in grep results. Writes still go through the guarded
self-edit pipeline — this module only reads.
"""

import asyncio
from pathlib import Path

from chief.agent.tools import Tool, ToolRegistry
from chief.provider.base import ToolSpec

# Narrower than the self-edit write guard (which also blocks ``data``):
# discovery needs to read package manifests under ``data/packages``.
READ_FORBIDDEN_PREFIXES = ("secrets", ".git")

_READ_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read a repo file's contents — inspect source before you self_edit, "
        "or read a package's manifest.yaml / INSTALL.md. Path is repo-relative."
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
        "Search repo files for a pattern (git grep). Optional `path` narrows "
        "to a subtree. Returns file:line matches."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    },
    read_only=True,
)


def _reject_read_path(path: str) -> str | None:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        return f"path escapes the harness: {path}"
    if p.parts and p.parts[0] in READ_FORBIDDEN_PREFIXES:
        return f"path is off-limits: {path}"
    return None


def register_file_tools(registry: ToolRegistry, root: Path) -> None:
    """Expose read_file + grep, rooted at the repo working tree."""

    async def read_file(path: str) -> str:
        if reason := _reject_read_path(path):
            return f"error: {reason}"
        target = root / path
        if not target.is_file():
            return f"error: no such file: {path}"
        return target.read_text()

    async def grep(pattern: str, path: str | None = None) -> str:
        if path is not None and (reason := _reject_read_path(path)):
            return f"error: {reason}"
        # Explicit argv (never a shell string), so the pattern cannot inject.
        argv = ["git", "grep", "--no-index", "-n", "-e", pattern]
        if path is not None:
            argv += ["--", path]
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await process.communicate()
        text = out.decode(errors="replace")
        # git grep exits 1 with no output when nothing matched.
        if process.returncode not in (0, 1):
            return f"error: grep failed: {text.strip()}"
        matches = [
            line
            for line in text.splitlines()
            if not any(
                line.startswith(f"{prefix}/") for prefix in READ_FORBIDDEN_PREFIXES
            )
        ]
        return "\n".join(matches) if matches else "no matches"

    registry.register(Tool(_READ_SPEC, read_file))
    registry.register(Tool(_GREP_SPEC, grep))
