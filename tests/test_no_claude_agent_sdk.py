"""Guard: no chief source imports claude-agent-sdk (#88).

The claude-agent-sdk backend, its gate/hook vocabulary, its classifier ``query()``, and
its in-process tool seam were all removed — the GitHub Copilot SDK is chief's sole
harness. This test fails the moment any file under ``src/`` re-introduces a
``claude_agent_sdk`` import, so the removal can't silently regress. It scans source text
(no import side effects) and covers ``import claude_agent_sdk`` and
``from claude_agent_sdk[...] import ...`` in every shape.
"""

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_PATTERN = re.compile(r"\bclaude_agent_sdk\b")


def test_no_source_file_imports_claude_agent_sdk() -> None:
    offenders = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if _PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "claude-agent-sdk was removed in #88 — these src files reference it again: "
        + ", ".join(offenders)
    )
