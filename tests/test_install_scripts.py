"""Shell-level tests for the installer scripts (#154).

bootstrap.sh is written as sourceable functions plus a guarded ``main``, so the
testable seams — OS detection, release-pinned fetch — run here as real bash
against fixture repos and fake binaries, without touching the machine. The whole
files also get a syntax pass and the shellcheck static gate (the same gate CI
runs).
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_BOOTSTRAP = _REPO_ROOT / "bootstrap.sh"
_INSTALL = _REPO_ROOT / "install.sh"

_HAS_SHELLCHECK = (
    subprocess.run(
        ["bash", "-c", "command -v shellcheck"], capture_output=True
    ).returncode
    == 0
)


def _bash(
    script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source bootstrap.sh (guard skips main) and run ``script`` in bash."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; source {shlex.quote(str(_BOOTSTRAP))}; {script}",
        ],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-b", "main", cwd=origin)
    (origin / "install.sh").write_text("#!/bin/bash\n")
    _git("add", "install.sh", cwd=origin)
    _git("commit", "-m", "v1", cwd=origin)
    _git("tag", "v0.1.0", cwd=origin)
    (origin / "install.sh").write_text("#!/bin/bash\n# v2\n")
    _git("commit", "-am", "v2", cwd=origin)
    _git("tag", "v0.2.0", cwd=origin)
    return origin


def _head_tag(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_scripts_parse(tmp_path: Path) -> None:
    for script in (_BOOTSTRAP, _INSTALL):
        subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(not _HAS_SHELLCHECK, reason="shellcheck not installed")
def test_shellcheck_static_gate() -> None:
    """The PRD's cheap CI gate: both installer scripts pass shellcheck."""
    result = subprocess.run(
        ["shellcheck", str(_BOOTSTRAP), str(_INSTALL)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout


def test_help_flags_exit_zero() -> None:
    for script in (_BOOTSTRAP, _INSTALL):
        result = subprocess.run(
            ["bash", str(script), "--help"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout or "install" in result.stdout


def test_unknown_flag_fails() -> None:
    result = subprocess.run(
        ["bash", str(_BOOTSTRAP), "--bogus"], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "unknown flag" in result.stderr


def test_detect_os_darwin_and_debian_and_other(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"

    uname.write_text('#!/bin/sh\necho "Darwin"\n')
    uname.chmod(0o755)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    assert _bash("detect_os", env).stdout.strip() == "darwin"

    uname.write_text('#!/bin/sh\necho "Linux"\n')
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nID_LIKE=debian\n')
    env["CHIEF_OS_RELEASE"] = str(os_release)
    assert _bash("detect_os", env).stdout.strip() == "debian"

    os_release.write_text("ID=fedora\n")
    assert _bash("detect_os", env).stdout.strip() == "linux-other"


def test_fetch_repo_clones_at_latest_tag(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"

    result = _bash(
        f"fetch_repo {shlex.quote(str(clone))}",
        env={"CHIEF_REPO_URL": f"file://{origin}"},
    )

    assert result.returncode == 0, result.stderr
    assert _head_tag(clone) == "v0.2.0", "must pin to the newest tag, not tip"


def test_fetch_repo_rerun_updates_to_newer_tag(tmp_path: Path) -> None:
    """Idempotent re-run: an existing clone fetches and moves to the new tag."""
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    env = {"CHIEF_REPO_URL": f"file://{origin}"}
    _bash(f"fetch_repo {shlex.quote(str(clone))}", env=env)

    (origin / "install.sh").write_text("#!/bin/bash\n# v3\n")
    _git("commit", "-am", "v3", cwd=origin)
    _git("tag", "v0.3.0", cwd=origin)
    result = _bash(f"fetch_repo {shlex.quote(str(clone))}", env=env)

    assert result.returncode == 0, result.stderr
    assert _head_tag(clone) == "v0.3.0"


def test_fetch_repo_respects_explicit_ref(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"

    result = _bash(
        f"fetch_repo {shlex.quote(str(clone))} v0.1.0",
        env={"CHIEF_REPO_URL": f"file://{origin}"},
    )

    assert result.returncode == 0, result.stderr
    assert _head_tag(clone) == "v0.1.0"


def test_fetch_repo_never_destroys_local_changes(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    env = {"CHIEF_REPO_URL": f"file://{origin}"}
    _bash(f"fetch_repo {shlex.quote(str(clone))}", env=env)
    (clone / "install.sh").write_text("# owner-edited\n")

    result = _bash(f"fetch_repo {shlex.quote(str(clone))}", env=env)

    assert result.returncode == 0, result.stderr
    assert (clone / "install.sh").read_text() == "# owner-edited\n"
    assert "leaving the current checkout" in result.stdout


def test_fetch_repo_tag_ordering_is_version_sort(tmp_path: Path) -> None:
    """v0.10.0 must beat v0.9.0 (version sort, not lexicographic)."""
    origin = _make_origin(tmp_path)
    _git("tag", "v0.9.0", cwd=origin)
    (origin / "install.sh").write_text("#!/bin/bash\n# ten\n")
    _git("commit", "-am", "ten", cwd=origin)
    _git("tag", "v0.10.0", cwd=origin)
    clone = tmp_path / "clone"

    result = _bash(
        f"fetch_repo {shlex.quote(str(clone))}",
        env={"CHIEF_REPO_URL": f"file://{origin}"},
    )

    assert result.returncode == 0, result.stderr
    assert _head_tag(clone) == "v0.10.0"


def test_fetch_repo_without_tags_warns_and_uses_tip(tmp_path: Path) -> None:
    """Pre-first-release repos install from the default branch, loudly."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-b", "main", cwd=origin)
    (origin / "readme").write_text("x\n")
    _git("add", "readme", cwd=origin)
    _git("commit", "-m", "init", cwd=origin)
    clone = tmp_path / "clone"

    result = _bash(
        f"fetch_repo {shlex.quote(str(clone))}",
        env={"CHIEF_REPO_URL": f"file://{origin}"},
    )

    assert result.returncode == 0, result.stderr
    assert "no release tags yet" in result.stdout
    assert (clone / "readme").is_file()
