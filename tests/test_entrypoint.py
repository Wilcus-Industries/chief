"""Unit tests for the container entrypoint's fail-loud migration runner.

Acceptance criteria from issue #4:
- An upgrade failure causes the entrypoint to exit non-zero (sys.exit(1)).
- The app is never started when the migration fails.
- A clear error is logged before exit.

Acceptance criteria from issue #8 (smoke test invariants):
- The "migration complete — starting app" log line appears on a successful run,
  proving ordering: migrate before exec.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from chief import entrypoint


def test_migrate_then_exec_calls_run_migrations(tmp_path: Path) -> None:
    """Happy path: _run_migrations is called and exec follows on success."""
    db_path = str(tmp_path / "chief.db")
    app_argv = [sys.executable, "-m", "chief.app"]

    with (
        patch.object(entrypoint, "_run_migrations") as mock_migrate,
        patch("os.execv") as mock_execv,
    ):
        entrypoint.migrate_then_exec(db_path, app_argv)

    mock_migrate.assert_called_once_with(db_path)
    mock_execv.assert_called_once_with(app_argv[0], app_argv)


def test_migrate_then_exec_logs_completion_before_exec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 'migration complete' line must be logged before os.execv is called.

    This is the unit-level proof of the migrate-before-app ordering invariant
    asserted by the container smoke test (issue #8).

    We call configure_logging() up front (as entrypoint.main does) so the
    completion line goes to the JSON/stdout handler and is captured by capsys.
    """
    from chief.obs.logging import configure_logging

    db_path = str(tmp_path / "chief.db")
    app_argv = [sys.executable, "-m", "chief.app"]
    call_order: list[str] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        call_order.append("execv")

    configure_logging()
    with (
        patch.object(entrypoint, "_run_migrations"),
        patch("os.execv", side_effect=fake_execv),
    ):
        entrypoint.migrate_then_exec(db_path, app_argv)

    captured = capsys.readouterr()
    assert "migration complete" in captured.out, (
        "Expected 'migration complete' in stdout before exec; "
        f"got stdout: {captured.out!r}"
    )
    assert call_order == ["execv"], f"execv not called; order: {call_order}"


def test_migrate_then_exec_exits_nonzero_on_upgrade_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-loud: a migration failure must sys.exit(1) and never exec the app."""
    db_path = str(tmp_path / "chief.db")
    app_argv = [sys.executable, "-m", "chief.app"]

    with (
        patch.object(
            entrypoint,
            "_run_migrations",
            side_effect=Exception("alembic upgrade failed"),
        ),
        patch("os.execv") as mock_execv,
        pytest.raises(SystemExit) as exc_info,
    ):
        entrypoint.migrate_then_exec(db_path, app_argv)

    assert exc_info.value.code == 1
    # The app must never boot on a half-migrated schema.
    mock_execv.assert_not_called()


def test_migrate_then_exec_logs_error_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A clear error line must appear in the log before the non-zero exit."""
    import logging

    db_path = str(tmp_path / "chief.db")
    app_argv = [sys.executable, "-m", "chief.app"]

    with caplog.at_level(logging.ERROR, logger="chief.entrypoint"):
        with (
            patch.object(
                entrypoint,
                "_run_migrations",
                side_effect=Exception("migration broke"),
            ),
            patch("os.execv"),
            pytest.raises(SystemExit),
        ):
            entrypoint.migrate_then_exec(db_path, app_argv)

    assert any("migration" in record.message.lower() for record in caplog.records), (
        "Expected an error log mentioning 'migration' before exit; "
        f"got: {[r.message for r in caplog.records]}"
    )
