"""Package conventions: manifest parsing, dependency order, agent tools."""

from pathlib import Path

import pytest

from chief.agent.tools import ToolRegistry
from chief.packages import PackageLibrary, register_package_tools
from chief.provider.base import ToolCall

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


async def test_package_tools_list_and_info(tmp_path: Path) -> None:
    write_package(tmp_path, "screen", "the screen")
    write_package(tmp_path, "top", "the top", deps=["screen"])
    registry = ToolRegistry()
    register_package_tools(
        registry, PackageLibrary((tmp_path,)), "https://example.com/pkgs"
    )

    listing = await registry.dispatch(
        ToolCall(id="1", name="list_packages", arguments={})
    )
    assert "- top: the top" in listing
    assert "https://example.com/pkgs" in listing

    info = await registry.dispatch(
        ToolCall(id="2", name="package_info", arguments={"name": "top"})
    )
    assert "install order (dependencies first): screen -> top" in info
    assert "# Installing top" in info

    missing = await registry.dispatch(
        ToolCall(id="3", name="package_info", arguments={"name": "nope"})
    )
    assert missing.startswith("error:")


def test_bundled_build_imessage_depends_on_screening() -> None:
    library = PackageLibrary((REPO_PACKAGES,))
    order = [p.name for p in library.install_order("build-imessage")]
    assert order == ["screening", "build-imessage"]
    package = library.get("build-imessage")
    assert package is not None
    assert "Full Disk Access" in package.install_md()
    assert "skills/build-imessage" in package.skills
