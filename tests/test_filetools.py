"""read_file + grep: the read-only repo window and its path guard."""

from pathlib import Path

from chief.agent.tools import ToolRegistry
from chief.filetools import register_file_tools
from chief.provider.base import ToolCall


def make_registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, root)
    return registry


async def test_read_file_returns_contents(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi there")
    result = await make_registry(tmp_path).dispatch(
        ToolCall(id="1", name="read_file", arguments={"path": "hello.txt"})
    )
    assert result == "hi there"


async def test_read_file_missing_is_error(tmp_path: Path) -> None:
    result = await make_registry(tmp_path).dispatch(
        ToolCall(id="1", name="read_file", arguments={"path": "nope.txt"})
    )
    assert result.startswith("error: no such file")


async def test_read_file_rejects_secrets_and_escapes(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    for bad in ("secrets/key", "../outside", "/etc/passwd", ".git/config"):
        result = await registry.dispatch(
            ToolCall(id="1", name="read_file", arguments={"path": bad})
        )
        assert result.startswith("error:"), bad


async def test_read_tools_are_read_only(tmp_path: Path) -> None:
    assert all(s.read_only for s in make_registry(tmp_path).specs())


async def test_grep_finds_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    result = await make_registry(tmp_path).dispatch(
        ToolCall(id="1", name="grep", arguments={"pattern": "target"})
    )
    assert "a.py" in result
    assert "def target" in result


async def test_grep_no_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here\n")
    result = await make_registry(tmp_path).dispatch(
        ToolCall(id="1", name="grep", arguments={"pattern": "zzznotfound"})
    )
    assert result == "no matches"


async def test_grep_omits_secrets(tmp_path: Path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "k").write_text("token PASSWORD\n")
    (tmp_path / "code.py").write_text("PASSWORD = env\n")
    result = await make_registry(tmp_path).dispatch(
        ToolCall(id="1", name="grep", arguments={"pattern": "PASSWORD"})
    )
    assert "code.py" in result
    assert "secrets" not in result
