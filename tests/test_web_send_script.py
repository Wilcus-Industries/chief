"""The web client's send script (``script_send.py``), run under node.

Skipped where node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from chief.web.pages import CHAT_PAGE
from chief.web.script import SCRIPT
from chief.web.script_send import PENDING_SCRIPT

NODE = shutil.which("node")
# Bound at import: a fake subprocess.run from another test file can outlive its
# test in a full-suite run, and this must reach the real node.
_RUN = subprocess.run
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(program: str) -> Any:
    assert NODE is not None
    done = _RUN(
        [NODE, "-e", program], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(done.stdout)


def test_the_served_script_parses(tmp_path: Path) -> None:
    assert NODE is not None
    path = tmp_path / "app.js"
    path.write_text(SCRIPT)
    _RUN([NODE, "--check", str(path)], check=True, timeout=30)


def test_settle_drops_the_oldest_match_only() -> None:
    out = run(
        PENDING_SCRIPT
        + """
const list = [{text: "a"}, {text: "b"}, {text: "a"}];
const hit = settlePending(list, "a");
const miss = settlePending(list, "zzz");
process.stdout.write(JSON.stringify({hit, miss, left: list.map((p) => p.text)}));
"""
    )
    assert out == {"hit": True, "miss": False, "left": ["b", "a"]}


def test_page_has_the_pending_container() -> None:
    assert '<div id="queued"></div>' in CHAT_PAGE
