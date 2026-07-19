"""The host shell tool: per-thread persistent shells spawned by the daemon.

Drives real shell subprocesses through :class:`ShellService` the way the agent loop
does — via ``ShellService.run`` and the registered ``shell`` tool — covering the
semantics ported from the old sandbox: thread isolation, state persistence,
timeout-with-kill, output cap, syntax pre-check, and shell-death reporting.
"""

from pathlib import Path

import pytest

from chief.agent.tools import ToolContext, ToolRegistry
from chief.provider.base import ToolCall
from chief.shellframe import (
    SHELL_DIED_EXIT_CODE,
    SYNTAX_ERROR_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    resolve_shell,
)
from chief.shellhost import ShellHost
from chief.shellprompt import host_label, shell_label, shell_prompt_line
from chief.shelltool import (
    ShellService,
    format_shell_result,
    register_shell_tool,
)


@pytest.fixture
def shell(tmp_path: Path) -> ShellService:
    """A ShellService rooted at tmp_path with tight limits (fast tests)."""
    return ShellService(
        workspace_dir=str(tmp_path / "ws"), timeout_seconds=2.0, output_limit=10_000
    )


def _call(command: str) -> ToolCall:
    return ToolCall(id="1", name="shell", arguments={"command": command})


# ---- result formatting ----------------------------------------------------------


def test_format_result_stdout_only() -> None:
    text = format_shell_result(
        {"stdout": "hi", "stderr": "", "exit_code": 0, "truncated": False}
    )
    assert text == "hi"


def test_format_result_marks_errors_and_truncation() -> None:
    text = format_shell_result(
        {"stdout": "", "stderr": "boom", "exit_code": 2, "truncated": True}
    )
    assert "boom" in text
    assert "exit code 2" in text
    assert "truncated" in text


def test_format_result_no_output() -> None:
    assert format_shell_result({}) == "(no output)"


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


def test_shell_label_is_the_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert shell_label() == "bash"


def test_shell_prompt_line_names_the_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")
    line = shell_prompt_line()
    assert "`shell` tool" in line
    assert "bash" in line
    assert "chief-pkg" in line


def test_host_label_maps_darwin_to_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    monkeypatch.setattr("platform.mac_ver", lambda: ("14.5", ("", "", ""), ""))
    assert host_label() == "macOS 14.5"


def test_host_label_linux_names_the_distro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "platform.freedesktop_os_release", lambda: {"PRETTY_NAME": "Arch Linux"}
    )
    assert host_label() == "Arch Linux"


def test_host_label_linux_falls_back_when_no_os_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")

    def _raise() -> dict[str, str]:
        raise OSError("no /etc/os-release")

    monkeypatch.setattr("platform.freedesktop_os_release", _raise)
    assert host_label() == "Linux"


def test_shell_prompt_line_names_the_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "platform.freedesktop_os_release", lambda: {"PRETTY_NAME": "Ubuntu 22.04"}
    )
    assert "Ubuntu 22.04" in shell_prompt_line()


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


async def test_state_persists_within_a_thread(shell: ShellService) -> None:
    await shell.run("s1", "export WHO=alice && mkdir -p sub && cd sub")
    result = await shell.run("s1", "echo $WHO $(basename $(pwd))")
    assert result["stdout"] == "alice sub"
    await shell.aclose()


async def test_thread_keys_isolate_two_threads(shell: ShellService) -> None:
    await shell.run("thread-a", "export WHO=alice")
    await shell.run("thread-b", "export WHO=bob")
    a = await shell.run("thread-a", "echo $WHO")
    b = await shell.run("thread-b", "echo $WHO")
    assert a["stdout"] == "alice"
    assert b["stdout"] == "bob"
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


async def test_per_call_timeout_overrides_service_default(tmp_path: Path) -> None:
    # A long service default must not stop a short per-call timeout from killing a hang
    # — this keeps one wedged command (e.g. a stuck `chief-pkg` clone) from blocking the
    # daemon for the whole service timeout.
    shell = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=120.0, output_limit=10_000
    )
    result = await shell.run("s1", "sleep 30", timeout=0.4)
    assert result["exit_code"] == TIMEOUT_EXIT_CODE
    await shell.aclose()


def test_shell_spec_exposes_optional_timeout() -> None:
    from chief.shelltool import _SHELL_SPEC

    props = _SHELL_SPEC.parameters["properties"]
    assert "timeout" in props
    assert "timeout" not in _SHELL_SPEC.parameters["required"]


async def test_registered_tool_passes_timeout_through(tmp_path: Path) -> None:
    # The agent-supplied `timeout` arg reaches the shell: a 0.4s cap kills `sleep 30`
    # instead of waiting out the 120s service default.
    shell = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=120.0, output_limit=10_000
    )
    registry = ToolRegistry()
    register_shell_tool(registry, shell)
    call = ToolCall(
        id="1", name="shell", arguments={"command": "sleep 30", "timeout": 0.4}
    )
    out = await registry.dispatch(call, ToolContext("t1", "socket"))
    assert f"exit code {TIMEOUT_EXIT_CODE}" in out
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


async def test_syntax_check_timeout_lets_the_command_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A parse-check process that never returns must not hang the real command forever
    # — it is killed by its own bounded timeout and treated as "no syntax error found".
    from chief import shellframe

    hang_script = tmp_path / "hang_shell.sh"
    hang_script.write_text("#!/bin/sh\nsleep 30\n")
    hang_script.chmod(0o755)
    monkeypatch.setattr(shellframe, "_SYNTAX_CHECK_TIMEOUT_SECONDS", 0.2)

    result = await shellframe._shell_syntax_error((str(hang_script),), "echo hi")

    assert result is None


async def test_shell_death_reports_distinct_code(shell: ShellService) -> None:
    result = await shell.run("s1", "exit 7")
    assert result["exit_code"] == SHELL_DIED_EXIT_CODE

    # A fresh shell serves the next command.
    after = await shell.run("s1", "echo alive")
    assert after["stdout"] == "alive"
    await shell.aclose()


# ---- the registered tool surface ------------------------------------------------


async def test_registered_tool_runs_on_host(shell: ShellService) -> None:
    registry = ToolRegistry()
    register_shell_tool(registry, shell)
    assert "shell" in {spec.name for spec in registry.specs()}
    out = await registry.dispatch(_call("echo from-tool"), ToolContext("t1", "socket"))
    assert out == "from-tool"
    await shell.aclose()


async def test_registered_tool_is_gated_not_read_only(shell: ShellService) -> None:
    # shell mutates the host — it must not be read-only, or the gate would auto-approve
    # every command without ever raising a card.
    registry = ToolRegistry()
    register_shell_tool(registry, shell)
    spec = next(s for s in registry.specs() if s.name == "shell")
    assert spec.read_only is False
    await shell.aclose()


async def test_registered_tool_isolates_by_thread_context(shell: ShellService) -> None:
    registry = ToolRegistry()
    register_shell_tool(registry, shell)
    await registry.dispatch(_call("export WHO=alice"), ToolContext("task-a", "socket"))
    await registry.dispatch(_call("export WHO=bob"), ToolContext("task-b", "socket"))
    a = await registry.dispatch(_call("echo $WHO"), ToolContext("task-a", "socket"))
    b = await registry.dispatch(_call("echo $WHO"), ToolContext("task-b", "socket"))
    assert a == "alice"
    assert b == "bob"
    await shell.aclose()


async def test_registered_tool_surfaces_spawn_failure(tmp_path: Path) -> None:
    # Point the service at an unspawnable shell: the tool surfaces the failure as an
    # error string instead of raising into the loop.
    service = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=1.0, output_limit=100
    )
    service._host = ShellHost(
        workdir=str(tmp_path),
        timeout=1.0,
        output_limit=100,
        argv=("/nonexistent/shell",),
    )
    registry = ToolRegistry()
    register_shell_tool(registry, service)
    out = await registry.dispatch(_call("echo hi"), ToolContext("t1", "socket"))
    assert "unavailable" in out


async def test_shell_guard_blocks_before_execution(tmp_path: Path) -> None:
    # A tripping guard refuses the command without ever reaching the shell
    # (audit C1: mechanical seatbelts over the raw command string).
    shell = ShellService(
        workspace_dir=str(tmp_path), timeout_seconds=5.0, output_limit=10_000
    )
    registry = ToolRegistry()

    def no_marker(command: str) -> str | None:
        return "error: blocked by guard" if "MARKER" in command else None

    register_shell_tool(registry, shell, guards=(no_marker,))
    blocked = await registry.dispatch(
        ToolCall(
            id="1", name="shell", arguments={"command": "touch MARKER && echo hi"}
        ),
        ToolContext("t1", "socket"),
    )
    assert blocked == "error: blocked by guard"
    assert not (tmp_path / "MARKER").exists()
    allowed = await registry.dispatch(
        ToolCall(id="2", name="shell", arguments={"command": "echo fine"}),
        ToolContext("t1", "socket"),
    )
    assert "fine" in allowed
    await shell.aclose()
