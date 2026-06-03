"""JSON log formatting to stdout."""

import json
import logging

import pytest

from chief.obs.logging import configure_logging


def _last_json_line(captured: str) -> dict[str, object]:
    lines = [line for line in captured.splitlines() if line.strip()]
    record: dict[str, object] = json.loads(lines[-1])
    return record


def test_emits_json_with_core_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level=logging.INFO)
    logging.getLogger("chief.test").info("hello", extra={"task_id": 7})

    record = _last_json_line(capsys.readouterr().out)
    assert record["level"] == "INFO"
    assert record["logger"] == "chief.test"
    assert record["message"] == "hello"
    assert record["task_id"] == 7
    assert "ts" in record


def test_includes_exception_text(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level=logging.INFO)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("chief.test").exception("failed")

    record = _last_json_line(capsys.readouterr().out)
    assert record["level"] == "ERROR"
    assert "boom" in str(record["exc"])


def test_configure_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    configure_logging()
    logging.getLogger("chief.test").warning("once")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1  # no duplicate handlers
