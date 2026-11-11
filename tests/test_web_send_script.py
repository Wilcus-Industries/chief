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
from chief.web.script_send import KEYS_SCRIPT, PENDING_SCRIPT

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


def test_reconcile_settles_only_entries_history_shows() -> None:
    """Two identical sends both saw one prior "ok"; history now has two, so
    exactly the older one landed."""
    out = run(
        PENDING_SCRIPT
        + """
const list = [{text: "ok", seen: 1}, {text: "ok", seen: 1}, {text: "new", seen: 0}];
reconcilePending(list, ["ok", "hello", "ok"]);
process.stdout.write(JSON.stringify(list.map((p) => p.text)));
"""
    )
    assert out == ["ok", "new"]


def test_page_has_the_pending_container() -> None:
    assert '<div id="queued"></div>' in CHAT_PAGE


def _key(event: str, touch: str = "false") -> Any:
    return run(
        KEYS_SCRIPT
        + f"process.stdout.write(JSON.stringify(composerKey({event}, {touch}) ?? null));"
    )


def test_enter_sends_and_shift_enter_is_a_newline() -> None:
    assert _key('{key: "Enter"}') == "send"
    assert _key('{key: "Enter", shiftKey: true}') is None  # native newline
    assert _key('{key: "a"}') is None


def test_ctrl_or_cmd_enter_interrupts() -> None:
    assert _key('{key: "Enter", ctrlKey: true}') == "interrupt"
    assert _key('{key: "Enter", metaKey: true}') == "interrupt"


def test_enter_interrupts_on_touch_and_ime_composition_is_left_alone() -> None:
    assert _key('{key: "Enter"}', "true") == "interrupt"
    assert _key('{key: "Enter", isComposing: true}') is None


def test_composer_is_a_multiline_textarea_with_a_send_button() -> None:
    assert '<textarea id="input"' in CHAT_PAGE
    assert 'id="sendbtn"' in CHAT_PAGE
