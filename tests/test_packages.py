"""Package conventions: manifest parsing and two-root scan (bundled wins)."""

from pathlib import Path

from chief.packages import PackageLibrary

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


def test_bundled_packages_carry_skill_and_install_md() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    for name in ("screening", "build-imessage"):
        package = library.get(name)
        assert package is not None, name
        assert f"skills/{name}" in package.skills
        assert package.install_md().strip()
