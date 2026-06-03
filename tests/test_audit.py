"""Append-only JSONL audit sink."""

import json
from pathlib import Path

from chief.obs.audit import AuditLog


def test_log_writes_one_parseable_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).log({"event": "tool_call", "tool": "Bash"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "tool_call"
    assert record["tool"] == "Bash"
    assert "ts" in record


def test_log_appends_and_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "audit.jsonl"
    log = AuditLog(path)

    log.log({"event": "a"})
    log.log({"event": "b"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


def test_log_serializes_non_json_values(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).log({"event": "x", "obj": object()})

    record = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(record["obj"], str)  # default=str fallback
