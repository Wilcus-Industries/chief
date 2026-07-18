"""The guarded restart pipeline: the central self-edit mechanism, over a real
(temporary) git repo — real done-check, real commit, real rollback marker."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from chief.agent.tools import ToolRegistry
from chief.audit import AuditLog
from chief.filetools import register_file_tools
from chief.provider.base import ToolCall
from chief.selfedit.pipeline import SelfEditPipeline
from chief.selfedit.recovery import (
    MARKER_NAME,
    RestartController,
    clear_marker,
    rollback_if_marked,
)
from chief.selfedit.tools import register_restart_tool


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
        repo, AuditLog(tmp_path / "audit.jsonl"), restart, checks=((check,),)
    )
    return pipeline, restart


# --- restart pipeline: green / red / no-op ---------------------------------


async def test_green_check_commits_and_restarts(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    (repo / "greeting.txt").write_text("hello v2\n")
    result = await pipeline.restart("improve the greeting")
    assert "restarting" in result
    assert (repo / "greeting.txt").read_text() == "hello v2\n"
    assert restart.called
    marker = json.loads((repo / MARKER_NAME).read_text())
    assert marker["rationale"] == "improve the greeting"
    assert "self-edit: improve the greeting" in git(repo, "log", "--oneline")


async def test_red_check_keeps_edits_and_reports(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("broken\n")
    result = await pipeline.restart("break it")
    assert result.startswith("error: done-check failed")
    # The edit is KEPT so the agent can fix forward — not reverted (#198).
    assert (repo / "greeting.txt").read_text() == "broken\n"
    assert not restart.called
    assert not (repo / MARKER_NAME).exists()
    assert "self-edit" not in git(repo, "log", "--oneline")


async def test_noop_restart_is_allowed(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    result = await pipeline.restart("just reload")
    assert "restarting" in result
    assert restart.called
    assert not (repo / MARKER_NAME).exists()
    assert "self-edit" not in git(repo, "log", "--oneline")


async def test_lingering_marker_is_not_a_change(repo: Path, tmp_path: Path) -> None:
    # The pipeline's own untracked marker must not read as a repo change and
    # get committed as an empty edit — the state that stranded the mini.
    (repo / MARKER_NAME).write_text('{"rollback_to": "x"}')
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    result = await pipeline.restart("reload")
    assert "no repo changes" in result
    assert restart.called
    assert "self-edit" not in git(repo, "log", "--oneline")


async def test_restart_is_serialized(repo: Path, tmp_path: Path) -> None:
    # Concurrent restarts must not interleave commit/status and corrupt git.
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    (repo / "greeting.txt").write_text("v2\n")
    results = await asyncio.gather(pipeline.restart("a"), pipeline.restart("b"))
    assert all("restarting" in r for r in results)
    assert restart.called
    assert git(repo, "log", "--oneline").count("self-edit") == 1


# --- central mechanism: write_file -> restart through the tools -------------


def _edit_registry(
    repo: Path, tmp_path: Path, check: str
) -> tuple[ToolRegistry, RestartSpy]:
    registry = ToolRegistry()
    register_file_tools(registry, repo)
    pipeline, restart = make_pipeline(repo, tmp_path, check)
    register_restart_tool(registry, pipeline)
    return registry, restart


async def test_write_then_restart_commits(repo: Path, tmp_path: Path) -> None:
    registry, restart = _edit_registry(repo, tmp_path, "true")
    await registry.dispatch(
        ToolCall(
            id="1",
            name="write_file",
            arguments={"path": "greeting.txt", "content": "agent wrote this\n"},
        )
    )
    result = await registry.dispatch(
        ToolCall(id="2", name="restart", arguments={"rationale": "agent edit"})
    )
    assert "restarting" in result
    assert (repo / "greeting.txt").read_text() == "agent wrote this\n"
    assert restart.called
    assert "self-edit: agent edit" in git(repo, "log", "--oneline")


async def test_write_then_failed_restart_keeps_edit(
    repo: Path, tmp_path: Path
) -> None:
    registry, restart = _edit_registry(repo, tmp_path, "false")
    await registry.dispatch(
        ToolCall(
            id="1",
            name="write_file",
            arguments={"path": "greeting.txt", "content": "still here\n"},
        )
    )
    result = await registry.dispatch(
        ToolCall(id="2", name="restart", arguments={"rationale": "nope"})
    )
    assert result.startswith("error: done-check failed")
    assert (repo / "greeting.txt").read_text() == "still here\n"
    assert not restart.called


# --- recovery.py: rollback marker + restart controller ---------------------


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
    """A crash during boot (incl. a broken import) rolls back and re-execs."""
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


async def test_restart_controller_drains_active_turns_before_firing() -> None:
    fired: list[bool] = []
    controller = RestartController(lambda: fired.append(True))
    await controller.enter_turn()
    controller.request()
    fire = asyncio.create_task(controller.fire_if_requested())
    await asyncio.sleep(0.01)
    assert not fired
    controller.leave_turn()
    await asyncio.wait_for(fire, 1)
    assert fired == [True]


async def test_restart_controller_holds_new_turns_once_requested() -> None:
    controller = RestartController(lambda: None)
    controller.request()
    entering = asyncio.create_task(controller.enter_turn())
    await asyncio.sleep(0.01)
    assert not entering.done()
    entering.cancel()


async def test_restart_controller_times_out_a_stuck_turn() -> None:
    fired: list[bool] = []
    controller = RestartController(lambda: fired.append(True), drain_timeout=0.02)
    await controller.enter_turn()
    controller.request()
    await asyncio.wait_for(controller.fire_if_requested(), 1)
    assert fired == [True]


async def test_restart_controller_noop_without_request() -> None:
    fired: list[bool] = []
    controller = RestartController(lambda: fired.append(True))
    await controller.fire_if_requested()
    assert fired == []
