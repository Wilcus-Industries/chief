"""chief-pkg discovery over real package roots and a real (local) clone root."""

import subprocess
from pathlib import Path

import pytest
import yaml

from chief import registry_apply
from chief.packages import Package, PackageLibrary
from chief.pkgcli import discover, render, verify_install
from chief.pkgsync import clone_if_missing, pull_clone


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


def test_pull_clone_is_a_no_op_without_a_clone(tmp_path: Path) -> None:
    assert pull_clone(tmp_path / "nothing") == "no clone to update"


def test_pull_clone_survives_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chief-pkg runs through the single dispatcher, so a hang here hangs the
    daemon — the exact failure an unbounded clone caused once already."""
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 10)

    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert "timed out" in pull_clone(dest)


def test_pull_clone_survives_a_failed_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diverged or offline clone degrades to stale, never to an exception."""
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "not possible to fast-forward")

    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert "could not pull" in pull_clone(dest)


def test_pull_clone_reports_what_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[-2:] == ["pull", "--ff-only"]
        return subprocess.CompletedProcess(cmd, 0, "Updating a1b2c3..d4e5f6\n", "")

    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert pull_clone(dest) == "Updating a1b2c3..d4e5f6"


# --- registry_apply + verify: enforced install postconditions (audit H1) ----


def test_registry_apply_sets_and_removes_entries(tmp_path: Path) -> None:
    registry = tmp_path / "data" / "installed.yaml"
    registry_apply.apply("screening", "bundled", registry)
    registry_apply.apply("memory", "cloned", registry)
    data = yaml.safe_load(registry.read_text())
    assert data == {
        "screening": {"source": "bundled"},
        "memory": {"source": "cloned"},
    }
    # Idempotent re-run and removal preserve the other entries.
    registry_apply.apply("screening", "bundled", registry)
    registry_apply.apply("memory", "cloned", registry, remove=True)
    assert yaml.safe_load(registry.read_text()) == {
        "screening": {"source": "bundled"}
    }


def test_registry_apply_survives_malformed_registry(tmp_path: Path) -> None:
    registry = tmp_path / "installed.yaml"
    registry.write_text("- not\n- a\n- mapping\n")
    registry_apply.apply("screening", "bundled", registry)
    assert yaml.safe_load(registry.read_text()) == {
        "screening": {"source": "bundled"}
    }


def test_registry_apply_rewrites_corrupt_yaml_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # apply() reads through load_installed, so a corrupt registry degrades
    # the same LOUD way everywhere instead of a raw traceback (#234) — and
    # the rewrite is the documented remedy.
    registry = tmp_path / "installed.yaml"
    registry.write_text("a: [unclosed\n")
    with caplog.at_level("ERROR"):
        registry_apply.apply("screening", "bundled", registry)
    assert "not valid yaml" in caplog.text
    assert yaml.safe_load(registry.read_text()) == {
        "screening": {"source": "bundled"}
    }


def _manifest_package(tmp_path: Path) -> Package:
    root = tmp_path / "packages" / "demo"
    root.mkdir(parents=True)
    # Path-form skill entry, like every real manifest (skills/<name>) — the
    # verify must compare on the basename.
    (root / "manifest.yaml").write_text(
        "name: demo\ndescription: d\nskills: [skills/demo]\n"
        "config_keys: [demo_block]\nsecrets: [demo_key]\n"
    )
    return PackageLibrary((tmp_path / "packages",)).scan()[0]


def test_verify_install_reports_every_missing_postcondition(
    tmp_path: Path,
) -> None:
    package = _manifest_package(tmp_path)
    problems = verify_install(
        package,
        installed={},
        config_raw={},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    text = "\n".join(problems)
    assert "not registered" in text
    assert "skill 'demo' missing" in text
    assert "config key 'demo_block' absent" in text
    assert "secret file 'demo_key' absent" in text


def test_verify_install_passes_when_everything_landed(tmp_path: Path) -> None:
    package = _manifest_package(tmp_path)
    (tmp_path / "skills" / "demo").mkdir(parents=True)
    (tmp_path / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "demo_key").write_text("k\n")
    problems = verify_install(
        package,
        installed={"demo": {"source": "bundled"}},
        config_raw={"demo_block": {}},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    assert problems == []


def _dotted_package(tmp_path: Path) -> Package:
    """A package declaring a nested config key — what real manifests use."""
    root = tmp_path / "packages" / "demo"
    root.mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "name: demo\ndescription: d\nskills: []\n"
        "config_keys: [imessage.enabled]\nsecrets: []\n"
    )
    return PackageLibrary((tmp_path / "packages",)).scan()[0]


def test_verify_install_resolves_dotted_config_keys(tmp_path: Path) -> None:
    # config_keys are dotted paths but config_raw is nested, so a flat
    # membership test called every real key absent — build-imessage could
    # never verify as installed however correct the config was.
    problems = verify_install(
        _dotted_package(tmp_path),
        installed={"demo": {"source": "bundled"}},
        config_raw={"imessage": {"enabled": True, "owner_handles": ["+1"]}},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    assert problems == []


def test_verify_install_still_reports_a_genuinely_missing_dotted_key(
    tmp_path: Path,
) -> None:
    package = _dotted_package(tmp_path)
    absent: tuple[dict[str, object], ...] = (
        {},
        {"imessage": {}},
        {"imessage": {"other": 1}},
    )
    for config in absent:
        problems = verify_install(
            package,
            installed={"demo": {"source": "bundled"}},
            config_raw=config,
            skills_root=tmp_path / "skills",
            secrets_root=tmp_path / "secrets",
        )
        assert problems == ["config key 'imessage.enabled' absent from config.yaml"]


def test_verify_install_reports_dotted_key_whose_parent_is_not_a_mapping(
    tmp_path: Path,
) -> None:
    # A scalar where a block belongs is a misconfiguration, not a crash.
    problems = verify_install(
        _dotted_package(tmp_path),
        installed={"demo": {"source": "bundled"}},
        config_raw={"imessage": "on"},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    assert problems == ["config key 'imessage.enabled' absent from config.yaml"]


# --- registry loading is loud when it degrades to empty (audit M4) ----------


def test_load_installed_absent_or_blank_is_quietly_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from chief.registry_apply import load_installed as _load_installed

    blank = tmp_path / "installed.yaml"
    blank.write_text("")
    with caplog.at_level("ERROR"):
        assert _load_installed(tmp_path / "missing.yaml") == {}
        assert _load_installed(blank) == {}
    assert not caplog.records  # absence is normal, not an error


def test_load_installed_non_mapping_is_empty_but_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from chief.registry_apply import load_installed as _load_installed

    registry = tmp_path / "installed.yaml"
    registry.write_text("- not\n- a\n- mapping\n")
    with caplog.at_level("ERROR"):
        assert _load_installed(registry) == {}
    assert "not a mapping" in caplog.text
    assert str(registry) in caplog.text


def test_load_installed_invalid_yaml_is_empty_but_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from chief.registry_apply import load_installed as _load_installed

    registry = tmp_path / "installed.yaml"
    registry.write_text("a: [unclosed\n")
    with caplog.at_level("ERROR"):
        assert _load_installed(registry) == {}
    assert "not valid yaml" in caplog.text
    assert str(registry) in caplog.text


def test_verify_install_checks_python_deps_importable(tmp_path: Path) -> None:
    root = tmp_path / "packages" / "depdemo"
    root.mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "name: depdemo\ndescription: d\n"
        "python_deps: [yaml, definitely_missing_dep_xyz]\n"
    )
    package = PackageLibrary((tmp_path / "packages",)).scan()[0]
    problems = verify_install(
        package,
        installed={"depdemo": {"source": "bundled"}},
        config_raw={},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    text = "\n".join(problems)
    assert "definitely_missing_dep_xyz" in text
    assert "import names" in text  # the fix hint names the semantics
    assert not any("'yaml'" in p for p in problems)  # importable dep passes


def test_verify_install_handles_dotted_dep_with_absent_parent(
    tmp_path: Path,
) -> None:
    # find_spec("missing.sub") raises ModuleNotFoundError rather than
    # returning None — that must read as "not importable", not crash (#234).
    root = tmp_path / "packages" / "dotdemo"
    root.mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "name: dotdemo\ndescription: d\n"
        "python_deps: [definitely_missing_dep_xyz.sub]\n"
    )
    package = PackageLibrary((tmp_path / "packages",)).scan()[0]
    problems = verify_install(
        package,
        installed={"dotdemo": {"source": "bundled"}},
        config_raw={},
        skills_root=tmp_path / "skills",
        secrets_root=tmp_path / "secrets",
    )
    assert any("definitely_missing_dep_xyz.sub" in p for p in problems)
