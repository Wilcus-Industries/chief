"""Deterministic package install: install.sh runs under the seatbelt (#185).

The model no longer retypes skill files; ``install_package`` runs each
package's ``install.sh`` inside the self-edit pipeline, copying files
byte-for-byte and setting config keys deterministically.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from chief.agent.tools import ToolRegistry
from chief.audit import AuditLog
from chief.config import merge_config
from chief.config_apply import main as config_apply_main
from chief.packages import PackageLibrary
from chief.provider.base import ToolCall
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.tools import register_install_tool

REAL_REPO = Path(__file__).parent.parent


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


class RestartSpy:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> None:
        self.called = True


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean pre-install git repo: the real packages/ plus an empty skills/.

    skills/ is seeded as a fixed placeholder, NOT copied from the live tree.
    It is the mutable *output* of an install: during a real install's done-check
    the just-copied package sits committed in skills/ (pipeline commits before
    checking), so a fixture mirroring the live tree baked that package into its
    baseline and the rollback assertions (skills/<pkg> absent) failed — the
    #185 first-run bug where installing any skill-package broke its own check.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _copytree(REAL_REPO / "packages", root / "packages")
    (root / "skills").mkdir()
    (root / "skills" / "README.md").write_text("skills\n")
    (root / "config.yaml").write_text("")
    (root / ".gitignore").write_text("config.yaml\ndata/\n")
    _make_uv_shim(root / "bin")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@test")
    git(root, "config", "user.name", "test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


def _copytree(src: Path, dst: Path) -> None:
    for path in src.rglob("*"):
        if path.is_file():
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def _make_uv_shim(bindir: Path) -> None:
    """A `uv run python …` shim that execs the test interpreter directly."""
    bindir.mkdir(parents=True)
    shim = bindir / "uv"
    shim.write_text(f'#!/usr/bin/env bash\nshift 2\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)


def _pipeline(repo: Path, tmp_path: Path, check: str = "true") -> SelfEditPipeline:
    return SelfEditPipeline(
        repo, AuditLog(tmp_path / "audit.jsonl"), RestartSpy(), checks=((check,),)
    )


def _install_env(repo: Path, **extra: str) -> dict[str, str]:
    return {"PATH": f"{repo / 'bin'}:{os.environ['PATH']}", **extra}


# --- fixture hermeticity ----------------------------------------------------


def test_fixture_baseline_has_no_preinstalled_packages(repo: Path) -> None:
    """The fixture must seed skills/ independently of the live working tree.

    Guards the #185 first-run bug: when a real install runs the done-check,
    skills/ holds the just-copied package (committed before the check). A
    fixture that mirrored that tree baked the package into its baseline, so the
    rollback assertions below (skills/<pkg> absent after a failed install) saw a
    pre-existing file and failed — installing any skill-package broke its own
    done-check. The package sources live under packages/; skills/ starts clean.
    """
    for pkg in ("build-imessage", "screening"):
        assert (repo / "packages" / pkg).is_dir()
        assert not (repo / "skills" / pkg).exists()


# --- merge_config / config_apply -------------------------------------------


def test_merge_config_creates_and_deep_merges(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    merge_config({"imessage": {"enabled": True}}, path)
    merge_config({"imessage": {"owner_handles": ["+1"]}, "web_port": 9}, path)
    loaded = yaml.safe_load(path.read_text())
    assert loaded == {
        "imessage": {"enabled": True, "owner_handles": ["+1"]},
        "web_port": 9,
    }


def test_config_apply_parses_dotted_yaml_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_apply_main(["imessage.enabled=true", 'imessage.owner_handles=["+1","+2"]'])
    loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert loaded == {"imessage": {"enabled": True, "owner_handles": ["+1", "+2"]}}


# --- pipeline.install seatbelt ---------------------------------------------


async def test_install_copies_skill_byte_for_byte(repo: Path, tmp_path: Path) -> None:
    pipeline = _pipeline(repo, tmp_path)
    script = Path("packages/screening/install.sh")
    result = await pipeline.install([script], _install_env(repo), "install screening")
    assert "restarting" in result
    installed = repo / "skills/screening/SKILL.md"
    source = repo / "packages/screening/skills/screening/SKILL.md"
    assert installed.read_bytes() == source.read_bytes()


async def test_failed_check_rolls_back_the_copy(repo: Path, tmp_path: Path) -> None:
    pipeline = _pipeline(repo, tmp_path, check="false")
    script = Path("packages/screening/install.sh")
    result = await pipeline.install([script], _install_env(repo), "install screening")
    assert result.startswith("error: done-check failed")
    assert not (repo / "skills/screening/SKILL.md").exists()
    assert "selfedit" not in git(repo, "branch", "--list")


async def test_failing_installer_reverts_and_reports(
    repo: Path, tmp_path: Path
) -> None:
    # build-imessage's install.sh aborts when IMESSAGE_HANDLES is unset.
    pipeline = _pipeline(repo, tmp_path)
    script = Path("packages/build-imessage/install.sh")
    with pytest.raises(RuntimeError, match="installer"):
        await pipeline.install([script], _install_env(repo), "install build-imessage")
    assert not (repo / "skills/build-imessage/SKILL.md").exists()
    assert "selfedit" not in git(repo, "branch", "--list")


# --- install_package tool ---------------------------------------------------


def _tool_registry(repo: Path, tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    library = PackageLibrary((repo / "packages",))
    register_install_tool(registry, _pipeline(repo, tmp_path), library)
    return registry


async def test_tool_installs_full_dependency_tree(repo: Path, tmp_path: Path) -> None:
    registry = _tool_registry(repo, tmp_path)
    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="install_package",
            arguments={
                "name": "build-imessage",
                "env": _install_env(repo, IMESSAGE_HANDLES="+15551234567"),
            },
        )
    )
    assert "restarting" in result
    for name in ("screening", "build-imessage"):
        installed = repo / f"skills/{name}/SKILL.md"
        source = repo / f"packages/{name}/skills/{name}/SKILL.md"
        assert installed.read_bytes() == source.read_bytes()
    config = yaml.safe_load((repo / "config.yaml").read_text())
    assert config["imessage"] == {"enabled": True, "owner_handles": ["+15551234567"]}


async def test_tool_rejects_unknown_package(repo: Path, tmp_path: Path) -> None:
    registry = _tool_registry(repo, tmp_path)
    result = await registry.dispatch(
        ToolCall(id="1", name="install_package", arguments={"name": "ghost"})
    )
    assert result.startswith("error:")


async def test_tool_validates_env(repo: Path, tmp_path: Path) -> None:
    registry = _tool_registry(repo, tmp_path)
    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="install_package",
            arguments={"name": "screening", "env": {"K": 5}},
        )
    )
    assert result.startswith("error: env must map")


async def test_tool_errors_when_install_sh_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pkg = root / "packages" / "bare"
    pkg.mkdir(parents=True)
    (pkg / "manifest.yaml").write_text("name: bare\ndescription: no installer\n")
    (pkg / "INSTALL.md").write_text("# bare\n")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    registry = ToolRegistry()
    register_install_tool(
        registry, _pipeline(root, tmp_path), PackageLibrary((root / "packages",))
    )
    result = await registry.dispatch(
        ToolCall(id="1", name="install_package", arguments={"name": "bare"})
    )
    assert result.startswith("error: no install.sh")
