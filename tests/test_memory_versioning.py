"""Versioner: GitVersioner against a real tmp repo; NullVersioner no-ops."""

import asyncio
import shutil
from pathlib import Path

import pytest

from chief.memory.versioning import GitVersioner, NullVersioner

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")


async def _log(root: Path) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "log", "--pretty=%s",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().split() if proc.returncode == 0 else []


async def _commit_count(root: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "rev-list", "--count", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return int(out.decode().strip()) if proc.returncode == 0 else 0


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


@requires_git
async def test_concurrent_commits_never_fail_on_index_lock(tmp_path: Path) -> None:
    """N>=3 concurrent commit() calls on one shared GitVersioner must all succeed.

    Without the internal asyncio.Lock each caller races git's index.lock and raises
    RuntimeError ~4/5 of the time.  With the lock they serialize and all return cleanly.
    """
    versioner = GitVersioner(
        tmp_path, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()

    # Write N distinct files so each of the N coroutines has at least some dirty
    # state to commit (the first acquirer under the lock will stage them all, and
    # later ones become no-ops — that's fine; the key assertion is no RuntimeError).
    n = 5
    for i in range(n):
        (tmp_path / f"turn-{i}.md").write_text(f"content {i}")

    # Fire N concurrent commit() calls — must not raise.
    results = await asyncio.gather(
        *(versioner.commit(f"turn-{i}") for i in range(n)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"concurrent commits raised: {errors}"
    # At least one real commit must exist (all dirty files staged by winner).
    assert await _commit_count(tmp_path) >= 1


@requires_git
async def test_concurrent_commit_and_empty_commit_no_op(tmp_path: Path) -> None:
    """A concurrent empty-commit call while a real commit runs must not raise and
    must not produce a spurious extra commit (the no-op guard is preserved under lock).
    """
    versioner = GitVersioner(
        tmp_path, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()

    # One caller has a real write; the others race it with an empty tree.
    (tmp_path / "data.md").write_text("hello")

    results = await asyncio.gather(
        versioner.commit("real write"),
        versioner.commit("empty a"),
        versioner.commit("empty b"),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"concurrent commits (mixed) raised: {errors}"
    # Exactly one commit (the "real write"); the empty-commit callers are no-ops.
    assert await _commit_count(tmp_path) == 1


@requires_git
async def test_harness_versioner_stages_only_its_root(tmp_path: Path) -> None:
    """A GitVersioner rooted at data/harness/ must never see its siblings (#110).

    db_path and workspace_dir live outside harness_dir by construction; this proves
    a real repo rooted there stages only what's under it.
    """
    harness = tmp_path / "harness"
    (harness / "skills").mkdir(parents=True)
    (harness / "subagents").mkdir(parents=True)
    (harness / "skills" / "s.md").write_text("skill")
    (harness / "subagents" / "a.md").write_text("agent")
    # Siblings outside the harness root — must never be staged.
    (tmp_path / "chief.db").write_text("db")
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "x").write_text("scratch")

    versioner = GitVersioner(
        harness, author_name="chief", author_email="chief@localhost"
    )
    await versioner.init()
    await versioner.commit("chief: harness auto-save")

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(harness), "ls-files",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    tracked = sorted(out.decode().split())
    assert tracked == ["skills/s.md", "subagents/a.md"]


@requires_git
async def test_two_versioners_over_separate_roots_no_collision(
    tmp_path: Path,
) -> None:
    """Two GitVersioners over distinct roots must commit concurrently without
    tripping over each other's index.lock — each owns its own lock + repo (#110)."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    versioner_a = GitVersioner(
        root_a, author_name="chief", author_email="chief@localhost"
    )
    versioner_b = GitVersioner(
        root_b, author_name="chief", author_email="chief@localhost"
    )
    await versioner_a.init()
    await versioner_b.init()
    (root_a / "dirty.md").write_text("a")
    (root_b / "dirty.md").write_text("b")

    results = await asyncio.gather(
        versioner_a.commit("commit a"),
        versioner_b.commit("commit b"),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"separate-root commits raised: {errors}"
    assert await _commit_count(root_a) == 1
    assert await _commit_count(root_b) == 1
