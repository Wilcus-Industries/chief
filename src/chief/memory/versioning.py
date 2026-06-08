"""Local git versioning for the memory dir (DESIGN: Soul.md/memory git-tracked).

A :class:`Versioner` records one commit per logical write op so any change — a written
fact, a ``/forget``, a hand-edited ``Soul.md`` — is reversible (``git revert``). Two
implementations:

- :class:`GitVersioner` shells out to ``git`` against the memory repo. Identity is
  passed per-commit (``-c user.name=… -c user.email=…``) so no global git config is
  required, and an op that changes nothing is skipped rather than committed empty.
- :class:`NullVersioner` is the no-op default for tests and ``memory_git: false`` runs.

The remote push + encryption land at M13 on top of this; nothing here is thrown away.
"""

import asyncio
import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("chief.memory.versioning")


class Versioner(Protocol):
    """Records memory changes as reversible versions."""

    async def init(self) -> None:
        """Prepare the version store (idempotent)."""
        ...

    async def commit(self, message: str) -> None:
        """Persist the current memory state as one version labelled ``message``."""
        ...


class NullVersioner:
    """No-op versioner — the test/``memory_git: false`` default."""

    async def init(self) -> None:
        return None

    async def commit(self, message: str) -> None:
        return None


class GitVersioner:
    """Subprocess-``git`` versioner over the memory directory."""

    def __init__(
        self, root: str | Path, *, author_name: str, author_email: str
    ) -> None:
        self._root = Path(root)
        self._name = author_name
        self._email = author_email

    async def init(self) -> None:
        """``git init`` the memory dir unless it is already a repo."""
        if (self._root / ".git").exists():
            return
        self._root.mkdir(parents=True, exist_ok=True)
        await self._git("init")

    async def commit(self, message: str) -> None:
        """Stage everything and commit, skipping the op if nothing changed."""
        await self._git("add", "-A")
        if not (await self._git("status", "--porcelain")).strip():
            return  # no diff — a commit here would be empty/noise
        await self._git(
            "-c", f"user.name={self._name}",
            "-c", f"user.email={self._email}",
            "commit", "-m", message,
        )

    async def _git(self, *args: str) -> str:
        """Run ``git -C <root> <args>``; return stdout, raising on a nonzero exit."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(self._root), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip()
            logger.error("git %s failed: %s", args[0], detail)
            raise RuntimeError(f"git {args[0]} failed: {detail}")
        return out.decode(errors="replace")
