"""Self-edit pipeline: seatbelt behavior over a real (temporary) git repo."""

import json
import subprocess
from pathlib import Path

import pytest

from chief.audit import AuditLog
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import MARKER_NAME, clear_marker, rollback_if_marked
from chief.selfedit.tools import register_selfedit_tools


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@test")
    git(root, "config", "user.name", "test")
    (root / "greeting.txt").write_text("hello\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


class RestartSpy:
    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> None:
        self.called = True


def make_pipeline(
    repo: Path, tmp_path: Path, check: str
) -> tuple[SelfEditPipeline, RestartSpy]:
    restart = RestartSpy()
    pipeline = SelfEditPipeline(
        repo,
        AuditLog(tmp_path / "audit.jsonl"),
        restart,
        checks=((check,),),
    )
    return pipeline, restart


async def test_green_check_merges_and_restarts(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    result = await pipeline.apply(
        {"greeting.txt": "hello v2\n"}, "improve the greeting"
    )
    assert "restarting" in result
    assert (repo / "greeting.txt").read_text() == "hello v2\n"
    assert restart.called
    marker = json.loads((repo / MARKER_NAME).read_text())
    assert marker["rationale"] == "improve the greeting"
    log = git(repo, "log", "--oneline")
    assert "self-edit: improve the greeting" in log


async def test_red_check_reverts_everything(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "false")
    result = await pipeline.apply({"greeting.txt": "broken\n"}, "break it")
    assert result.startswith("error: done-check failed")
    assert (repo / "greeting.txt").read_text() == "hello\n"
    assert not restart.called
    assert not (repo / MARKER_NAME).exists()
    branches = git(repo, "branch", "--list")
    assert "selfedit" not in branches


async def test_dirty_tree_refuses(repo: Path, tmp_path: Path) -> None:
    (repo / "greeting.txt").write_text("uncommitted change\n")
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    result = await pipeline.apply({"greeting.txt": "x"}, "r")
    assert result == "error: working tree is dirty; refusing to self-edit"
    assert not restart.called


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside.txt", "secrets/openrouter_api_key", "data/chief.db"],
)
async def test_forbidden_paths_are_rejected(
    repo: Path, tmp_path: Path, path: str
) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    result = await pipeline.apply({path: "x"}, "sneaky")
    assert result.startswith("error:")
    assert not restart.called
    assert "self-edit" not in git(repo, "log", "--oneline")


def test_rollback_if_marked_resets_and_reexecs(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "greeting.txt").write_text("bad code\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "self-edit: bad")
    (repo / MARKER_NAME).write_text(json.dumps({"rollback_to": base}))
    assert rollback_if_marked(repo) is True
    assert (repo / "greeting.txt").read_text() == "hello\n"
    assert not (repo / MARKER_NAME).exists()
    assert rollback_if_marked(repo) is False


async def test_boot_failure_after_selfedit_rolls_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash during boot (incl. a broken import) rolls back and re-execs.

    ``build_app`` is imported inside ``amain``'s try, so an ``ImportError``
    from a self-edit that breaks ``chief.app`` follows the same rollback path
    as a runtime failure — it never escapes the seatbelt.
    """
    import chief.app
    import chief.entrypoint

    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "greeting.txt").write_text("bad code\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "self-edit: bad")
    (repo / MARKER_NAME).write_text(json.dumps({"rollback_to": base}))

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boot exploded")

    restarted = RestartSpy()
    monkeypatch.setattr(chief.app, "build_app", boom)
    monkeypatch.setattr(chief.entrypoint, "restart_daemon", restarted)
    monkeypatch.chdir(repo)

    with pytest.raises(RuntimeError):
        await chief.entrypoint.amain()

    assert (repo / "greeting.txt").read_text() == "hello\n"
    assert not (repo / MARKER_NAME).exists()
    assert restarted.called


def test_clear_marker_declares_health(repo: Path) -> None:
    (repo / MARKER_NAME).write_text(json.dumps({"rollback_to": "x"}))
    clear_marker(repo)
    assert not (repo / MARKER_NAME).exists()
    clear_marker(repo)  # idempotent when absent


async def test_tool_validates_its_arguments(repo: Path, tmp_path: Path) -> None:
    from chief.agent.tools import ToolRegistry
    from chief.provider.base import ToolCall

    pipeline, _ = make_pipeline(repo, tmp_path, "true")
    registry = ToolRegistry()
    register_selfedit_tools(registry, pipeline)
    bad = await registry.dispatch(
        ToolCall(
            id="1", name="self_edit", arguments={"files": "nope", "rationale": "r"}
        )
    )
    assert bad.startswith("error: files must be")
    empty = await registry.dispatch(
        ToolCall(id="2", name="self_edit", arguments={"files": {}, "rationale": "r"})
    )
    assert empty.startswith("error: files must be")
