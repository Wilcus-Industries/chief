"""The web client's buffer sidebar (``script_buffers.py``): pure helpers under
node, page wiring as text. Skipped without node."""

from typing import Any

from chief.web.pages import CHAT_PAGE
from chief.web.script_buffers import BUFFERS_PURE_SCRIPT

from .node_js import needs_node, run_js

pytestmark = needs_node


def call(expr: str) -> Any:
    program = f"process.stdout.write(JSON.stringify({expr}));"
    return run_js(BUFFERS_PURE_SCRIPT + program)


def test_only_web_scratch_buffers_offer_delete_matching_the_server() -> None:
    assert call('deletable({thread: "web:scratch", channel: "web"})')
    assert not call('deletable({thread: "web:main", channel: "web"})')
    assert not call('deletable({thread: "+15551234567", channel: "imessage"})')


def test_a_sidebar_snippet_drops_markdown_marks_and_line_breaks() -> None:
    text = "## Done\\n\\n**Disk** is `fine` \\u2014 [see](http://x)\\n- a"
    assert call(f'plainSnippet("{text}")') == "Done Disk is fine — see a"


def test_page_has_a_drawer_toggle_and_working_line() -> None:
    assert 'id="menu"' in CHAT_PAGE
    assert 'id="working"' in CHAT_PAGE
