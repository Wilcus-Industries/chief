"""Package conventions: manifest parsing and dependency order.

Discovery is skill-driven now (read_file/grep over manifests); the library
just parses and resolves install order for the install_package tool.
"""

from pathlib import Path

import pytest

from chief.packages import PackageLibrary

REPO_PACKAGES = Path(__file__).parent.parent / "packages"


def write_package(
    root: Path, name: str, description: str = "", deps: list[str] | None = None
) -> None:
    pkg = root / name
    pkg.mkdir(parents=True)
    lines = [f"name: {name}", f"description: {description}"]
    if deps:
        lines.append("dependencies: [" + ", ".join(deps) + "]")
    (pkg / "manifest.yaml").write_text("\n".join(lines) + "\n")
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


def test_install_order_puts_dependencies_first(tmp_path: Path) -> None:
    write_package(tmp_path, "screen")
    write_package(tmp_path, "mid", deps=["screen"])
    write_package(tmp_path, "top", deps=["mid", "screen"])
    library = PackageLibrary((tmp_path,))
    assert [p.name for p in library.install_order("top")] == ["screen", "mid", "top"]


def test_install_order_rejects_unknown_and_cycles(tmp_path: Path) -> None:
    write_package(tmp_path, "a", deps=["b"])
    write_package(tmp_path, "b", deps=["a"])
    library = PackageLibrary((tmp_path,))
    with pytest.raises(KeyError):
        library.install_order("ghost")
    with pytest.raises(ValueError, match="cycle"):
        library.install_order("a")


def test_bundled_build_imessage_depends_on_screening() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    order = [p.name for p in library.install_order("build-imessage")]
    assert order == ["screening", "build-imessage"]
    package = library.get("build-imessage")
    assert package is not None
    assert "Full Disk Access" in package.install_md()
    assert "skills/build-imessage" in package.skills


def test_bundled_memory_ships_skill_no_deps() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    package = library.get("memory")
    assert package is not None
    assert package.dependencies == ()
    assert "skills/memory" in package.skills
    assert "MEMORY.md" in package.install_md()


def test_bundled_soul_depends_on_memory() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    order = [p.name for p in library.install_order("soul")]
    assert order == ["memory", "soul"]
    package = library.get("soul")
    assert package is not None
    assert "skills/soul" in package.skills
    assert "Soul.md" in package.install_md()
