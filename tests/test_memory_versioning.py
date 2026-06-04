"""Versioner: GitVersioner against a real tmp repo; NullVersioner no-ops."""

import shutil
from pathlib import Path

import pytest

from chief.memory.versioning import GitVersioner, NullVersioner

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")


async def _log(root: Path) -> list[str]:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "log", "--pretty=%s",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().split() if proc.returncode == 0 else []


async def test_null_versioner_is_noop(tmp_path: Path) -> None:
    versioner = NullVersioner()
    await versioner.init()
    await versioner.commit("anything")  # must not raise, must not create a repo

    assert not (tmp_path / ".git").exists()


@requires_git
async def test_git_versioner_one_commit_per_op(tmp_path: Path) -> None:
    versioner = GitVersioner(
        tmp_path, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()
    assert (tmp_path / ".git").exists()

    (tmp_path / "a.md").write_text("one")
    await versioner.commit("write a")
    (tmp_path / "b.md").write_text("two")
    await versioner.commit("write b")

    assert await _log(tmp_path) == ["write", "b", "write", "a"]  # newest first


@requires_git
async def test_git_versioner_skips_empty_commit(tmp_path: Path) -> None:
    versioner = GitVersioner(
        tmp_path, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()
    (tmp_path / "a.md").write_text("one")
    await versioner.commit("write a")

    await versioner.commit("nothing changed")  # no diff → no new commit

    assert await _log(tmp_path) == ["write", "a"]


@requires_git
async def test_git_versioner_init_is_idempotent(tmp_path: Path) -> None:
    versioner = GitVersioner(
        tmp_path, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()
    (tmp_path / "a.md").write_text("one")
    await versioner.commit("write a")

    await versioner.init()  # re-init must not wipe history

    assert await _log(tmp_path) == ["write", "a"]
