"""The guarded restart pipeline: the central self-edit mechanism, over a real
(temporary) git repo — real done-check, real commit, real rollback marker."""

import asyncio
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from chief.agent.tools import ToolContext, ToolRegistry
from chief.audit import AuditLog
from chief.config import ConfigError
from chief.filetools import register_file_tools
from chief.provider.base import ToolCall
from chief.selfedit import gitops
from chief.selfedit.notice import (
    NOTICE_PATH,
    NOTICE_TTL_SECONDS,
    RestartNotice,
    mark_notice_rolled_back,
    take_restart_notice,
    write_restart_notice,
)
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
    """Stands in for RestartController.request (which takes the notice)."""

    def __init__(self) -> None:
        self.called = False
        self.notice: RestartNotice | None = None

    def __call__(self, notice: RestartNotice | None = None) -> None:
        self.called = True
        self.notice = notice


def make_pipeline(
    repo: Path,
    tmp_path: Path,
    check: str,
    validate: Callable[[], object] = lambda: None,
) -> tuple[SelfEditPipeline, RestartSpy]:
    restart = RestartSpy()
    pipeline = SelfEditPipeline(
        repo,
        AuditLog(tmp_path / "audit.jsonl"),
        restart,
        checks=((check,),),
        validate_config=validate,
    )
    return pipeline, restart


# --- restart pipeline: green / red / no-op ---------------------------------


async def test_green_check_commits_and_restarts(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    (repo / "greeting.txt").write_text("hello v2\n")
    # A dirty tree first echoes the changed-file list for review (audit C3).
    preview = await pipeline.restart("improve the greeting")
    assert preview.startswith("confirm:")
    assert "greeting.txt" in preview
    assert not restart.called
    result = await pipeline.restart("improve the greeting", confirm=True)
    assert "restarting" in result
    assert (repo / "greeting.txt").read_text() == "hello v2\n"
    assert restart.called
    marker = json.loads((repo / MARKER_NAME).read_text())
    assert marker["rationale"] == "improve the greeting"
    assert "self-edit: improve the greeting" in git(repo, "log", "--oneline")


async def test_red_check_keeps_edits_and_reports(repo: Path, tmp_path: Path) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("broken\n")
    result = await pipeline.restart("break it", confirm=True)
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


async def test_bad_config_aborts_restart_and_keeps_edits(
    repo: Path, tmp_path: Path
) -> None:
    # The done-check runs against fixtures; a bad *real* config.yaml value
    # passes every check then boot-loops launchd. Validate the live config
    # after the check and before commit/restart: abort, keep the edit, no
    # commit, no restart — mirror the red-check "edits kept" contract.
    def bad_config() -> object:
        raise ConfigError("owner_handles must be a quoted handle")

    pipeline, restart = make_pipeline(repo, tmp_path, "true", validate=bad_config)
    (repo / "greeting.txt").write_text("edited\n")
    result = await pipeline.restart("edit the greeting", confirm=True)
    assert result.startswith("error: config")
    assert not restart.called
    assert (repo / "greeting.txt").read_text() == "edited\n"  # kept, not reverted
    assert "self-edit" not in git(repo, "log", "--oneline")


async def test_real_bad_config_yaml_aborts_restart(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end via the default validator (load_config): a bare-int
    # owner_handles (YAML dropping the '+') is exactly the value that
    # boot-looped the mini. It must abort the restart here instead.
    (repo / "config.yaml").write_text("imessage:\n  owner_handles: 5551234567\n")
    restart = RestartSpy()
    pipeline = SelfEditPipeline(
        repo, AuditLog(tmp_path / "audit.jsonl"), restart, checks=(("true",),)
    )
    monkeypatch.chdir(repo)
    result = await pipeline.restart("some edit", confirm=True)
    assert result.startswith("error: config")
    assert not restart.called
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


async def test_confirm_preview_runs_no_checks(repo: Path, tmp_path: Path) -> None:
    # The file-list preview must come back before any (expensive) check runs:
    # with a failing check, a dirty unconfirmed restart still returns the
    # confirm message, not the check failure.
    pipeline, restart = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("dirty\n")
    result = await pipeline.restart("some change")
    assert result.startswith("confirm:")
    assert not restart.called


async def test_confirmed_commit_records_file_list(
    repo: Path, tmp_path: Path
) -> None:
    pipeline, _ = make_pipeline(repo, tmp_path, "true")
    (repo / "greeting.txt").write_text("v2\n")
    (repo / "extra.txt").write_text("debris\n")
    await pipeline.restart("labeled change", confirm=True)
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    restarting = [e for e in entries if e.get("outcome") == "restarting"]
    assert sorted(restarting[-1]["files"]) == ["extra.txt", "greeting.txt"]


async def test_failure_streak_escalates_after_three(
    repo: Path, tmp_path: Path
) -> None:
    # Circuit breaker (audit H3): the third consecutive red tells the agent to
    # stop editing forward and surface the diff / revert.
    pipeline, _ = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("broken\n")
    first = await pipeline.restart("try", confirm=True)
    second = await pipeline.restart("try again", confirm=True)
    third = await pipeline.restart("once more", confirm=True)
    assert "STOP" not in first and "STOP" not in second
    assert "STOP editing forward" in third
    assert "revert_edits" in third


async def test_green_resets_failure_streak(repo: Path, tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("broken\n")
    await pipeline.restart("a", confirm=True)
    await pipeline.restart("b", confirm=True)
    # Flip the check green: streak resets, so a later red starts from 1.
    pipeline._checks = (("true",),)
    await pipeline.restart("c", confirm=True)
    pipeline._checks = (("false",),)
    (repo / "greeting.txt").write_text("broken again\n")
    result = await pipeline.restart("d", confirm=True)
    assert "STOP" not in result


async def test_revert_edits_restores_tracked_keeps_untracked(
    repo: Path, tmp_path: Path
) -> None:
    pipeline, _ = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("broken\n")
    (repo / "notes.txt").write_text("untracked\n")
    result = await pipeline.revert_edits()
    assert (repo / "greeting.txt").read_text() == "hello\n"  # back to HEAD
    assert (repo / "notes.txt").exists()  # untracked left alone
    assert "greeting.txt" in result
    assert "notes.txt" in result


async def test_revert_edits_noop_on_clean_tree(repo: Path, tmp_path: Path) -> None:
    pipeline, _ = make_pipeline(repo, tmp_path, "true")
    assert "nothing to revert" in await pipeline.revert_edits()


async def test_revert_edits_discards_staged_content_too(
    repo: Path, tmp_path: Path
) -> None:
    # `git checkout -- .` restores from the INDEX: an edit staged via a shell
    # `git add` would survive the "revert". The tool promises HEAD (#234).
    pipeline, _ = make_pipeline(repo, tmp_path, "false")
    (repo / "greeting.txt").write_text("staged-broken\n")
    git(repo, "add", "greeting.txt")
    result = await pipeline.revert_edits()
    assert (repo / "greeting.txt").read_text() == "hello\n"  # back to HEAD
    assert git(repo, "status", "--porcelain").strip() == ""  # index reset too
    assert "greeting.txt" in result


async def test_dirty_files_reports_rename_destination(repo: Path) -> None:
    # Porcelain renames read `R  old -> new`; the confirm list should show
    # just the destination path, unquoted (#234, display-only).
    from chief.selfedit.gitops import dirty_files

    git(repo, "mv", "greeting.txt", "salute with space.txt")
    files = await dirty_files(repo)
    assert files == ["salute with space.txt"]


def test_capture_times_out_hung_check(tmp_path: Path) -> None:
    # A hung done-check must fail, not hold the restart lock forever (audit H2).
    code, output = gitops._capture(
        ("sleep", "60"), tmp_path, timeout=0.2
    )
    assert code == 1
    assert "timed out" in output


async def test_restart_is_serialized(repo: Path, tmp_path: Path) -> None:
    # Concurrent restarts must not interleave commit/status and corrupt git.
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    (repo / "greeting.txt").write_text("v2\n")
    results = await asyncio.gather(
        pipeline.restart("a", confirm=True), pipeline.restart("b", confirm=True)
    )
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
    preview = await registry.dispatch(
        ToolCall(id="2", name="restart", arguments={"rationale": "agent edit"})
    )
    assert preview.startswith("confirm:")
    assert "greeting.txt" in preview
    result = await registry.dispatch(
        ToolCall(
            id="3",
            name="restart",
            arguments={"rationale": "agent edit", "confirm": True},
        )
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
        ToolCall(
            id="2", name="restart", arguments={"rationale": "nope", "confirm": True}
        )
    )
    assert result.startswith("error: done-check failed")
    assert (repo / "greeting.txt").read_text() == "still here\n"
    assert not restart.called


# --- restart notice: reporting back to the requesting thread ---------------


async def test_restart_hands_the_requesting_thread_to_the_reboot(
    repo: Path, tmp_path: Path
) -> None:
    """The notice rides to the reboot — nothing on disk while the turn runs.

    Between request and re-exec sit the rest of the turn and the drain; a
    notice written here would be claimed by any unrelated reboot in that
    window.
    """
    pipeline, restart = make_pipeline(repo, tmp_path, "true")
    origin = ToolContext(thread_key="imessage:owner", channel="imessage")
    result = await pipeline.restart("config reload", origin=origin)
    assert "restarting" in result
    assert restart.called
    assert not (repo / NOTICE_PATH).exists()
    assert restart.notice is not None
    assert (restart.notice.channel, restart.notice.thread_key) == (
        "imessage",
        "imessage:owner",
    )
    assert "config reload" in restart.notice.text()


async def test_failed_check_hands_over_no_notice(
    repo: Path, tmp_path: Path
) -> None:
    pipeline, restart = make_pipeline(repo, tmp_path, "false")
    origin = ToolContext(thread_key="cli:t", channel="cli")
    await pipeline.restart("broken", confirm=True, origin=origin)
    assert not restart.called
    assert take_restart_notice(repo) is None


async def test_restart_tool_passes_its_thread_through(
    repo: Path, tmp_path: Path
) -> None:
    registry, restart = _edit_registry(repo, tmp_path, "true")
    await registry.dispatch(
        ToolCall(id="1", name="restart", arguments={"rationale": "reload"}),
        ToolContext(thread_key="web:main", channel="web"),
    )
    assert restart.notice is not None
    assert (restart.notice.channel, restart.notice.thread_key) == (
        "web",
        "web:main",
    )


async def test_controller_writes_the_notice_only_at_reboot_time(
    repo: Path,
) -> None:
    """The disk write and the re-exec must be adjacent, not a drain apart."""
    at_reboot: list[bool] = []
    controller = RestartController(
        lambda: at_reboot.append((repo / NOTICE_PATH).exists()), repo_root=repo
    )
    controller.request(RestartNotice("cli", "cli:t", "reload"))
    # Requested, turn still running: nothing on disk for a stray reboot yet.
    assert not (repo / NOTICE_PATH).exists()
    await controller.fire_if_requested()
    assert at_reboot == [True]
    notice = take_restart_notice(repo)
    assert notice is not None
    assert notice.thread_key == "cli:t"


async def test_controller_without_a_notice_writes_nothing(repo: Path) -> None:
    controller = RestartController(lambda: None, repo_root=repo)
    controller.request()
    await controller.fire_if_requested()
    assert take_restart_notice(repo) is None


def test_stale_notice_is_dropped_unread(repo: Path) -> None:
    """A reboot that never happened must not congratulate a later one."""
    write_restart_notice(repo, RestartNotice("imessage", "+1555", "old news"))
    path = repo / NOTICE_PATH
    data = json.loads(path.read_text())
    data["written_at"] = time.time() - (NOTICE_TTL_SECONDS + 1)
    path.write_text(json.dumps(data))
    assert take_restart_notice(repo) is None
    # Dropped, not left to be claimed by the boot after this one either.
    assert not path.exists()


def test_fresh_notice_survives_the_ttl_check(repo: Path) -> None:
    write_restart_notice(repo, RestartNotice("cli", "cli:t", "just now"))
    assert take_restart_notice(repo) is not None


def test_rolled_back_notice_reports_the_failure(repo: Path) -> None:
    write_restart_notice(
        repo, RestartNotice(channel="cli", thread_key="cli:t", rationale="risky")
    )
    mark_notice_rolled_back(repo)
    notice = take_restart_notice(repo)
    assert notice is not None
    assert notice.rolled_back
    assert notice.text().startswith("⚠️ restart failed")


def test_unreadable_notice_is_consumed_not_raised(repo: Path) -> None:
    path = repo / NOTICE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert take_restart_notice(repo) is None
    assert not path.exists()


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
