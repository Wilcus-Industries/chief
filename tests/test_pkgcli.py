"""chief-pkg discovery over real package roots and a real (local) clone root."""

import subprocess
from pathlib import Path

import pytest

from chief.pkgcli import clone_if_missing, discover, render


def write_package(root: Path, name: str, description: str) -> None:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "manifest.yaml").write_text(f"name: {name}\ndescription: {description}\n")
    (pkg / "INSTALL.md").write_text(f"# install {name}\n")
    (pkg / "UNINSTALL.md").write_text(f"# uninstall {name}\n")


def roots(tmp_path: Path) -> tuple[Path, Path]:
    bundled = tmp_path / "packages"
    clone = tmp_path / "data" / "packages"
    write_package(bundled, "screening", "message screening policy")
    write_package(clone, "memory", "durable notes")
    write_package(clone, "screening", "cloned dup that must lose")
    return bundled, clone


def test_discover_tags_source_and_bundled_wins(tmp_path: Path) -> None:
    bundled, clone = roots(tmp_path)
    rows = {r.name: r for r in discover(bundled, clone, {})}
    assert set(rows) == {"screening", "memory"}
    assert rows["screening"].source == "bundled"
    assert rows["screening"].description == "message screening policy"
    assert rows["memory"].source == "cloned"


def test_discover_marks_installed_from_registry(tmp_path: Path) -> None:
    bundled, clone = roots(tmp_path)
    installed = {"memory": {"source": "cloned", "commit": "abc123"}}
    rows = {r.name: r for r in discover(bundled, clone, installed)}
    assert rows["memory"].installed is True
    assert rows["screening"].installed is False


def test_render_shows_status_source_path(tmp_path: Path) -> None:
    bundled, clone = roots(tmp_path)
    rows = discover(bundled, clone, {"memory": {}})
    text = render(rows)
    assert "memory  [installed] (cloned)" in text
    assert "screening  [available] (bundled)" in text
    assert "durable notes" in text


def test_missing_clone_root_is_fine(tmp_path: Path) -> None:
    bundled = tmp_path / "packages"
    write_package(bundled, "screening", "only bundled")
    rows = discover(bundled, tmp_path / "nowhere", {})
    assert [r.name for r in rows] == ["screening"]


def test_clone_if_missing_survives_a_bad_remote(tmp_path: Path) -> None:
    dest = tmp_path / "clone"
    # A non-existent remote must warn, not raise — discovery falls back to
    # bundled-only.
    clone_if_missing("file:///no/such/repo.git", dest)
    assert not dest.exists()


def test_clone_if_missing_skips_when_present(tmp_path: Path) -> None:
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "keep").write_text("x")
    clone_if_missing("file:///no/such/repo.git", dest)
    assert (dest / "keep").read_text() == "x"


def test_clone_if_missing_cannot_hang_on_a_credential_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git must never block on an interactive auth prompt or a stalled network — that
    # would wedge `chief-pkg` and, through the single dispatcher, the whole daemon.
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    clone_if_missing("https://example.com/repo.git", tmp_path / "clone")
    assert captured.get("timeout")  # bounded, never unbounded
    env = captured.get("env")
    assert isinstance(env, dict) and env.get("GIT_TERMINAL_PROMPT") == "0"


def test_clone_if_missing_survives_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clone that hits its own timeout must warn and fall back to bundled-only, not
    # raise into the CLI.
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    dest = tmp_path / "clone"
    clone_if_missing("https://example.com/repo.git", dest)
    assert not dest.exists()
