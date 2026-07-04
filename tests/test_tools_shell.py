"""The host shell tool: per-session persistent shells spawned by the core process.

Drives real (bash) subprocesses through :class:`ShellService` the way the SDK and the
scheduler would — via the tool handler and :meth:`ShellService.run` — covering the
semantics ported from the old sandbox server: session isolation, state persistence,
timeout-with-kill, output cap, syntax pre-check, and shell-death reporting.
"""

from pathlib import Path

import pytest

from chief.tools.shell import (
    SHELL_DIED_EXIT_CODE,
    SYNTAX_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TOOL_NAME,
    ShellHost,
    ShellService,
    format_result,
    resolve_shell,
)


@pytest.fixture
def shell(tmp_path: Path) -> ShellService:
    """A ShellService rooted at tmp_path with tight limits (fast tests)."""
    return ShellService(
        workspace_dir=str(tmp_path / "ws"), timeout_seconds=2.0, output_limit=10_000
    )


def test_tool_name_is_sdk_qualified(shell: ShellService) -> None:
    assert shell.tool_name == TOOL_NAME == "mcp__chief_shell__bash"


def test_format_result_marks_errors_and_truncation() -> None:
    ok = format_result(
        {"stdout": "hi", "stderr": "", "exit_code": 0, "truncated": False}
    )
    assert ok["is_error"] is False
    assert ok["content"][0]["text"] == "hi"

    bad = format_result(
        {"stdout": "", "stderr": "boom", "exit_code": 2, "truncated": True}
    )
    assert bad["is_error"] is True
    text = bad["content"][0]["text"]
    assert "boom" in text and "exit code 2" in text and "truncated" in text


# ---- resolve_shell -------------------------------------------------------------


def test_resolve_shell_prefers_env_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")
    argv = resolve_shell()
    assert argv == ("/bin/bash", "--norc", "--noprofile")


def test_resolve_shell_falls_back_when_env_shell_is_bogus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/nonexistent/shell")
    argv = resolve_shell()
    assert Path(argv[0]).name in ("bash", "zsh")


def test_resolve_shell_zsh_gets_no_rcs_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the macOS default: $SHELL points at zsh (skip if not installed).
    import shutil

    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh not installed")
    monkeypatch.setenv("SHELL", zsh)
    assert resolve_shell() == (zsh, "-f")


# ---- command execution ----------------------------------------------------------


async def test_run_round_trip(shell: ShellService) -> None:
    result = await shell.run("s1", "echo hello")
    assert result["stdout"] == "hello"
    assert result["exit_code"] == 0
    await shell.aclose()


async def test_workspace_dir_is_created_and_is_cwd(
    shell: ShellService, tmp_path: Path
) -> None:
    result = await shell.run("s1", "pwd")
    assert result["stdout"] == str((tmp_path / "ws").resolve())
    assert (tmp_path / "ws").is_dir()
    await shell.aclose()


async def test_state_persists_within_a_session(shell: ShellService) -> None:
    await shell.run("s1", "export WHO=alice && mkdir -p sub && cd sub")
    result = await shell.run("s1", "echo $WHO $(basename $(pwd))")
    assert result["stdout"] == "alice sub"
    await shell.aclose()


async def test_session_keys_isolate_two_tasks(shell: ShellService) -> None:
    task_a = shell._build_bash_tool("task-a")
    task_b = shell._build_bash_tool("task-b")

    await task_a.handler({"command": "export WHO=alice"})
    await task_b.handler({"command": "export WHO=bob"})

    a = await task_a.handler({"command": "echo $WHO"})
    b = await task_b.handler({"command": "echo $WHO"})

    assert a["content"][0]["text"] == "alice"
    assert b["content"][0]["text"] == "bob"
    await shell.aclose()


async def test_nonzero_exit_code_reported(shell: ShellService) -> None:
    result = await shell.run("s1", "false")
    assert result["exit_code"] == 1
    await shell.aclose()


async def test_env_is_inherited(
    shell: ShellService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Full env inheritance is deliberate (host-native, owner-accepted).
    monkeypatch.setenv("CHIEF_TEST_MARKER", "inherited-42")
    result = await shell.run("env-check", "echo $CHIEF_TEST_MARKER")
    assert result["stdout"] == "inherited-42"
    await shell.aclose()


async def test_timeout_kills_and_respawns(tmp_path: Path) -> None:
    shell = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=0.4, output_limit=10_000
    )
    result = await shell.run("s1", "sleep 30")
    assert result["exit_code"] == TIMEOUT_EXIT_CODE

    # The hung shell was respawned: the next command runs on a fresh one.
    after = await shell.run("s1", "echo back")
    assert after["stdout"] == "back"
    assert after["exit_code"] == 0
    await shell.aclose()


async def test_output_cap_truncates(tmp_path: Path) -> None:
    shell = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=5.0, output_limit=100
    )
    result = await shell.run("s1", "yes chief | head -200")
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 100
    assert result["exit_code"] == 0  # the command itself completed
    await shell.aclose()


async def test_syntax_error_rejected_without_touching_the_shell(
    shell: ShellService,
) -> None:
    await shell.run("s1", "export KEEP=1")
    bad = await shell.run("s1", "echo 'unterminated")
    assert bad["exit_code"] == SYNTAX_ERROR_EXIT_CODE
    assert bad["stderr"]

    # The persistent shell was never disturbed: its state survives the rejection.
    kept = await shell.run("s1", "echo $KEEP")
    assert kept["stdout"] == "1"
    await shell.aclose()


async def test_dangling_line_continuation_rejected(shell: ShellService) -> None:
    result = await shell.run("s1", "echo hi \\")
    assert result["exit_code"] == SYNTAX_ERROR_EXIT_CODE
    await shell.aclose()


async def test_shell_death_reports_distinct_code(shell: ShellService) -> None:
    result = await shell.run("s1", "exit 7")
    assert result["exit_code"] == SHELL_DIED_EXIT_CODE

    # A fresh shell serves the next command.
    after = await shell.run("s1", "echo alive")
    assert after["stdout"] == "alive"
    await shell.aclose()


# ---- the SDK tool surface ---------------------------------------------------------


async def test_bash_tool_runs_on_host(shell: ShellService) -> None:
    bash = shell._build_bash_tool("t1")
    out = await bash.handler({"command": "echo from-tool"})

    assert out["is_error"] is False
    assert out["content"][0]["text"] == "from-tool"
    await shell.aclose()


async def test_bash_tool_surfaces_spawn_failure(tmp_path: Path) -> None:
    # Point the service at an unspawnable shell: the tool surfaces the failure
    # instead of raising into the SDK.
    service = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=1.0, output_limit=100
    )
    service._host = ShellHost(
        workdir=str(tmp_path),
        timeout=1.0,
        output_limit=100,
        argv=("/nonexistent/shell",),
    )
    bash = service._build_bash_tool("t1")
    out = await bash.handler({"command": "echo hi"})

    assert out["is_error"] is True
    assert "unavailable" in out["content"][0]["text"]
