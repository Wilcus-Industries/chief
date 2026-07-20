"""Package conventions: manifest parsing and two-root scan (bundled wins)."""

import re
import subprocess
from pathlib import Path

from chief.packages import HookSpec, PackageLibrary, validate

REPO_PACKAGES = Path(__file__).parent.parent / "packages"


def write_package(root: Path, name: str, description: str = "") -> None:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "manifest.yaml").write_text(f"name: {name}\ndescription: {description}\n")
    (pkg / "INSTALL.md").write_text(f"# Installing {name}\n")


def test_scan_merges_roots_and_bundled_wins_collisions(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    cloned = tmp_path / "cloned"
    write_package(bundled, "alpha", "bundled alpha")
    write_package(cloned, "alpha", "cloned alpha")
    write_package(cloned, "beta", "cloned beta")
    library = PackageLibrary((bundled, cloned))
    packages = {p.name: p for p in library.scan()}
    assert set(packages) == {"alpha", "beta"}
    assert packages["alpha"].description == "bundled alpha"


def test_missing_root_is_fine(tmp_path: Path) -> None:
    assert PackageLibrary((tmp_path / "nowhere",)).scan() == []


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    write_package(tmp_path, "solo")
    library = PackageLibrary((tmp_path,))
    assert library.get("ghost") is None
    assert library.get("solo") is not None


def test_manifest_hooks_block_parses_to_a_hookspec(tmp_path: Path) -> None:
    pkg = tmp_path / "withhooks"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text(
        "name: withhooks\ndescription: d\n"
        "hooks:\n  module: hooks.py\n  register: register\n"
    )
    package = PackageLibrary((tmp_path,)).get("withhooks")
    assert package is not None
    assert package.hooks == HookSpec(module="hooks.py", register="register")


def test_manifest_without_hooks_has_none(tmp_path: Path) -> None:
    write_package(tmp_path, "plain")
    assert PackageLibrary((tmp_path,)).get("plain").hooks is None  # type: ignore[union-attr]


def test_bundled_packages_carry_skill_and_install_md() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    for name in ("screening", "build-imessage"):
        package = library.get(name)
        assert package is not None, name
        assert f"skills/{name}" in package.skills
        assert package.install_md().strip()


def test_obsidian_memory_manifest_is_well_formed() -> None:
    # The first code-shipping hook package must validate with a well-formed
    # hooks block and its single top-level config key.
    assert validate((REPO_PACKAGES,)) == []
    package = PackageLibrary((REPO_PACKAGES,)).get("obsidian-memory")
    assert package is not None
    assert package.hooks == HookSpec(module="hooks.py", register="register")
    assert package.config_keys == ("obsidian_memory",)
    assert "skills/obsidian-memory" in package.skills
    assert package.install_md().strip()


def test_manifest_python_deps_parse_into_the_package(tmp_path: Path) -> None:
    pkg = tmp_path / "packages" / "depdemo"
    pkg.mkdir(parents=True)
    (pkg / "manifest.yaml").write_text(
        "name: depdemo\ndescription: d\npython_deps: [chromadb, networkx]\n"
    )
    package = PackageLibrary((tmp_path / "packages",)).scan()[0]
    assert package.python_deps == ("chromadb", "networkx")


# --- install.sh preflight: fail fast before any mutation (audit M1) ---------


def _run_install(script: Path, cwd: Path, env: dict[str, str]) -> object:
    import os
    import subprocess

    return subprocess.run(
        ["bash", str(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


def test_anthropic_oauth_install_fails_fast_without_secret(tmp_path: Path) -> None:
    # A missing bearer must abort before the skill copy or any config write —
    # wiring a dead setup into config.yaml while "succeeding" was audit M1.
    script = REPO_PACKAGES / "anthropic-oauth" / "install.sh"
    result = _run_install(
        script, tmp_path, {"PROXY_URL": "http://127.0.0.1:1/v1", "MODEL": "m"}
    )
    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "proxy_api_key" in result.stderr  # type: ignore[attr-defined]
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "skills").exists()


def test_anthropic_oauth_install_rejects_non_http_proxy_url(
    tmp_path: Path,
) -> None:
    # A PROXY_URL starting with a dash would be parsed as curl OPTIONS, not a
    # URL — require an http(s) scheme before anything runs (#234).
    script = REPO_PACKAGES / "anthropic-oauth" / "install.sh"
    result = _run_install(
        script, tmp_path, {"PROXY_URL": "-o/tmp/evil", "MODEL": "m"}
    )
    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "http" in result.stderr  # type: ignore[attr-defined]
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "skills").exists()


def test_anthropic_oauth_install_fails_fast_on_dead_proxy(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "proxy_api_key").write_text("k\n")
    script = REPO_PACKAGES / "anthropic-oauth" / "install.sh"
    result = _run_install(
        script, tmp_path, {"PROXY_URL": "http://127.0.0.1:1/v1", "MODEL": "m"}
    )
    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "did not answer" in result.stderr  # type: ignore[attr-defined]
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "skills").exists()


# --- installer static checks ------------------------------------------------
#
# Package installers are the one executable surface the done-check never runs:
# they are shell, they mutate the repo, and the agent self-edits them. A broken
# one stays silent until an owner installs. Both checks below are cheap and
# catch the two failure modes that actually shipped (see git log for the
# obsidian-memory installer chief edited into an unrunnable state).

# ${x:-}, ${x:?}, ${x:+} supply or demand a value, so an unassigned name there
# is deliberate — that is how an installer declares an env parameter.
_GUARDED = re.compile(r"\$\{(\w+)\s*:[-?+]")
_USED = re.compile(r"\$\{(\w+)[}\s:]|\$(\w+)")
# A bare NAME= anywhere, so `if ! served=$(...)` and `x=1; y=2` both count.
# Deliberately lax: over-counting an assignment only makes this check quieter,
# while under-counting would cry wolf on every installer.
_ASSIGNED = re.compile(r"(?<![\w$-])(\w+)=(?!=)")
_FOR = re.compile(r"^\s*for\s+(\w+)\s+in\b", re.M)
# `read -r a b c` binds every trailing name, not just the first.
_READ = re.compile(r"\bread\s+((?:-\w+\s+)*)([\w\s]+)")
# Set by the shell itself, never assigned by the script.
_SHELL_PROVIDED = {
    "BASH_SOURCE", "HOME", "IFS", "PATH", "PWD", "USER", "UID", "OSTYPE",
    "RANDOM", "LINENO", "FUNCNAME", "SHELL", "TMPDIR", "HOSTNAME", "PS1",
}


def _installers() -> list[Path]:
    found = sorted(REPO_PACKAGES.glob("*/install.sh"))
    assert found, "no package installers found — glob or layout changed"
    return found


def unguarded_undefined_vars(script: str) -> set[str]:
    """Names expanded bare (no ``:-``/``:?``/``:+``) and never assigned.

    This is the shape that broke the obsidian-memory installer: a self-edit
    deleted the loop building ``writable_yaml`` while leaving ``$writable_yaml``
    in the config_apply call below it. Under ``set -u`` that aborts the install.
    ``bash -n`` cannot see it — the syntax is perfectly valid.
    """
    guarded = set(_GUARDED.findall(script))
    assigned = set(_ASSIGNED.findall(script)) | set(_FOR.findall(script))
    for _flags, names in _READ.findall(script):
        assigned |= set(names.split())
    used = {a or b for a, b in _USED.findall(script)}
    return used - guarded - assigned - _SHELL_PROVIDED - {""}


def test_every_package_installer_is_syntactically_valid() -> None:
    broken = []
    for path in _installers():
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True
        )
        if result.returncode != 0:
            broken.append(f"{path.parent.name}: {result.stderr.strip()}")
    assert not broken, "installers with shell syntax errors:\n" + "\n".join(broken)


def test_every_package_installer_defines_the_vars_it_expands() -> None:
    offenders = []
    for path in _installers():
        missing = unguarded_undefined_vars(path.read_text())
        if missing:
            offenders.append(f"{path.parent.name}: {sorted(missing)}")
    assert not offenders, (
        "installers expanding names they never assign and do not guard with "
        "${x:-}/${x:?} — fatal under `set -u`:\n" + "\n".join(offenders)
    )


def test_the_check_catches_the_bug_that_shipped() -> None:
    """Guard the guard, using the real regression.

    Reduced from the installer the agent self-edited: the writable_yaml builder
    is gone, its use is not.
    """
    broken = (
        'set -euo pipefail\n'
        'WRITABLE_PATHS="${WRITABLE_PATHS:-}"\n'
        ': "${VAULT_PATH:?set VAULT_PATH}"\n'
        'uv run python -m chief.config_apply '
        '"obsidian_memory.writable_paths=$writable_yaml"\n'
    )
    assert unguarded_undefined_vars(broken) == {"writable_yaml"}
    # The env parameters it *does* guard are not flagged.
    assert "VAULT_PATH" not in unguarded_undefined_vars(broken)
    assert "WRITABLE_PATHS" not in unguarded_undefined_vars(broken)
