"""File tools: full-filesystem read/grep (read-only) and write/edit (gated)."""

from pathlib import Path

from chief.agent.tools import ToolRegistry
from chief.filetools import register_file_tools
from chief.provider.base import ToolCall


def make_registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, root)
    return registry


async def call(root: Path, name: str, **args: str) -> str:
    return await make_registry(root).dispatch(
        ToolCall(id="1", name=name, arguments=args)
    )


# --- read_file --------------------------------------------------------------


async def test_read_file_returns_contents(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi there")
    assert await call(tmp_path, "read_file", path="hello.txt") == "hi there"


async def test_read_file_missing_is_error(tmp_path: Path) -> None:
    result = await call(tmp_path, "read_file", path="nope.txt")
    assert result.startswith("error: no such file")


async def test_read_file_reaches_absolute_paths(tmp_path: Path) -> None:
    # Reads broaden to the full filesystem — no repo confinement, no carve-outs.
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("off-repo")
    result = await call(tmp_path / "repo", "read_file", path=str(outside))
    assert result == "off-repo"


# --- grep -------------------------------------------------------------------


async def test_grep_finds_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    result = await call(tmp_path, "grep", pattern="target")
    assert "a.py" in result
    assert "def target" in result


async def test_grep_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here\n")
    assert await call(tmp_path, "grep", pattern="zzznotfound") == "no matches"


async def test_grep_narrows_to_path(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("NEEDLE = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "other.py").write_text("NEEDLE = 2\n")
    result = await call(tmp_path, "grep", pattern="NEEDLE", path="sub")
    assert "other.py" in result
    assert "keep.py" not in result


async def test_grep_bad_pattern_is_error(tmp_path: Path) -> None:
    result = await call(tmp_path, "grep", pattern="(")
    assert result.startswith("error: bad pattern")


# --- write_file / edit_file -------------------------------------------------


async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    result = await call(tmp_path, "write_file", path="a/b/c.txt", content="deep")
    assert "wrote" in result
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "deep"


async def test_write_file_has_no_carve_outs(tmp_path: Path) -> None:
    # secrets/.git/data are writable by design — the gate is the only guard.
    for path in ("secrets/key", "data/state", ".git/hook"):
        result = await call(tmp_path, "write_file", path=path, content="x")
        assert "wrote" in result, path
        assert (tmp_path / path).read_text() == "x"


async def test_edit_file_replaces_all_occurrences(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a = old\nb = old\n")
    result = await call(tmp_path, "edit_file", path="f.py", old="old", new="new")
    assert "2 replacement" in result
    assert (tmp_path / "f.py").read_text() == "a = new\nb = new\n"


async def test_edit_file_missing_old_is_error(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("nothing\n")
    result = await call(tmp_path, "edit_file", path="f.py", old="ghost", new="x")
    assert result.startswith("error: `old` not found")
    assert (tmp_path / "f.py").read_text() == "nothing\n"


async def test_edit_file_missing_file_is_error(tmp_path: Path) -> None:
    result = await call(tmp_path, "edit_file", path="nope.py", old="a", new="b")
    assert result.startswith("error: no such file")


# --- read-only classification ----------------------------------------------


async def test_only_read_tools_are_read_only(tmp_path: Path) -> None:
    by_name = {s.name: s.read_only for s in make_registry(tmp_path).specs()}
    assert by_name == {
        "read_file": True,
        "grep": True,
        "write_file": False,
        "edit_file": False,
    }
