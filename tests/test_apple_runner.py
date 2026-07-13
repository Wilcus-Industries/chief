"""ScriptRunner tests (#155): the Apple family's real subprocess boundary.

The runner is the PRD's central mechanism — every Apple tool funnels through it as a
child process. These tests exercise the REAL ``asyncio.create_subprocess_exec`` path
against stub executables (tiny shell scripts), so the mechanism itself is never mocked;
only the macOS binaries are stand-ins. The per-app service suites then fake the runner
at this exact seam (argv + stdin in, ``ScriptResult`` out).
"""

import stat
from collections.abc import Sequence
from pathlib import Path

from chief.tools.apple.runner import (
    TIMEOUT_EXIT_CODE,
    ScriptResult,
    ScriptRunner,
    is_permission_denied,
    script_error_result,
    text_result,
)

# ---- stub executables --------------------------------------------------------


def _make_stub(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable shell script and return its absolute path."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# ---- run(): capture, exit codes, stdin ----------------------------------------


async def test_run_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    stub = _make_stub(tmp_path, "echoer", 'echo "out:$@"\necho "err" 1>&2\nexit 3')
    result = await ScriptRunner().run([stub, "a", "b"])
    assert result.stdout == "out:a b\n"
    assert result.stderr == "err\n"
    assert result.exit_code == 3
    assert not result.ok
    assert not result.timed_out


async def test_run_pipes_stdin_to_the_child(tmp_path: Path) -> None:
    stub = _make_stub(tmp_path, "catter", "cat")
    result = await ScriptRunner().run([stub], stdin=b"clipboard payload")
    assert result.stdout == "clipboard payload"
    assert result.exit_code == 0
    assert result.ok


async def test_run_missing_binary_reports_instead_of_raising() -> None:
    result = await ScriptRunner().run(["/nonexistent/osascript", "-e", "1"])
    assert result.exit_code == 127
    assert "not found" in result.stderr
    assert not result.ok


async def test_run_times_out_and_kills_the_child(tmp_path: Path) -> None:
    stub = _make_stub(tmp_path, "sleeper", "sleep 30")
    result = await ScriptRunner(timeout=0.2).run([stub])
    assert result.timed_out
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert "timed out" in result.stderr


async def test_run_caps_output_and_flags_truncation(tmp_path: Path) -> None:
    stub = _make_stub(tmp_path, "spewer", "head -c 10000 /dev/zero | tr '\\0' 'x'")
    result = await ScriptRunner(output_limit=100).run([stub])
    assert len(result.stdout) == 100
    assert result.truncated


# ---- convenience wrappers: exact argv construction -----------------------------


class _RecordingRunner(ScriptRunner):
    """Records the argv/stdin each call would exec (the fake-seam contract)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    async def run(
        self, argv: Sequence[str], *, stdin: bytes | None = None
    ) -> ScriptResult:
        self.calls.append((tuple(argv), stdin))
        return ScriptResult("", "", 0)


async def test_run_jxa_builds_the_exact_osascript_invocation() -> None:
    runner = _RecordingRunner()
    await runner.run_jxa("function run(argv) { return argv[0]; }", ["hello"])
    assert runner.calls == [
        (
            (
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
                "-e",
                "function run(argv) { return argv[0]; }",
                "hello",
            ),
            None,
        )
    ]


async def test_run_shortcuts_builds_the_exact_cli_invocation() -> None:
    runner = _RecordingRunner()
    await runner.run_shortcuts(["run", "Leaving work", "-o", "-"], stdin=b"input")
    assert runner.calls == [
        (("/usr/bin/shortcuts", "run", "Leaving work", "-o", "-"), b"input")
    ]


async def test_run_sqlite_builds_a_readonly_json_invocation() -> None:
    runner = _RecordingRunner()
    await runner.run_sqlite("/tmp/chat.db", "SELECT 1;")
    assert runner.calls == [
        (("/usr/bin/sqlite3", "-readonly", "-json", "/tmp/chat.db", "SELECT 1;"), None)
    ]


# ---- permission-denied classification ------------------------------------------


def test_automation_denial_shape_is_permission_denied() -> None:
    # Captured shape: osascript against an app without the Automation grant.
    result = ScriptResult(
        stdout="",
        stderr=(
            "execution error: Not authorized to send Apple events to "
            "Reminders. (-1743)"
        ),
        exit_code=1,
    )
    assert is_permission_denied(result)


def test_sqlite_full_disk_denial_shapes_are_permission_denied() -> None:
    # Captured shapes: sqlite3 against chat.db without Full Disk Access.
    for stderr in (
        'Error: unable to open database "/Users/w/Library/Messages/chat.db": '
        "unable to open database file",
        "Error: in prepare, authorization denied (23)",
    ):
        assert is_permission_denied(ScriptResult("", stderr, 1))


def test_success_and_plain_errors_are_not_permission_denied() -> None:
    assert not is_permission_denied(ScriptResult("ok", "", 0))
    assert not is_permission_denied(
        ScriptResult("", "execution error: some other problem (-2700)", 1)
    )


# ---- result rendering ------------------------------------------------------------


def test_text_result_shape() -> None:
    assert text_result("hi") == {
        "content": [{"type": "text", "text": "hi"}],
        "is_error": False,
    }
    assert text_result("bad", is_error=True)["is_error"] is True


def test_script_error_result_routes_denials_to_the_doctor() -> None:
    denied = ScriptResult(
        "", "execution error: Not authorized to send Apple events to Notes. "
        "(-1743)", 1
    )
    rendered = script_error_result("search Notes", denied)
    assert rendered["is_error"] is True
    text = rendered["content"][0]["text"]
    assert "check_apple_health" in text
    assert "search Notes" in text


def test_script_error_result_reports_timeouts_and_generic_failures() -> None:
    timed = ScriptResult("", "timed out after 30s", TIMEOUT_EXIT_CODE, timed_out=True)
    assert "timed out" in script_error_result("list events", timed)["content"][0][
        "text"
    ]
    generic = ScriptResult("", "boom", 2)
    assert "boom" in script_error_result("list events", generic)["content"][0]["text"]
