"""The web transcript's markdown parser (``script_markdown.py``), run under node.

The parser is pure (text -> ``{tag, children}`` tree), so it is exercised
without a DOM. Skipped where node is not installed.
"""

import json
import shutil
import subprocess
from typing import Any

import pytest

from chief.web.script import SCRIPT
from chief.web.script_markdown import MARKDOWN_SCRIPT

NODE = shutil.which("node")
# Bound at import: a fake subprocess.run from another test file can outlive its
# test in a full-suite run, and this must reach the real node.
_RUN = subprocess.run
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def parse(text: str) -> list[Any]:
    assert NODE is not None
    program = (
        MARKDOWN_SCRIPT
        + "\nconst input = require('fs').readFileSync(0, 'utf8');"
        + "\nprocess.stdout.write(JSON.stringify(mdBlocks(input)));"
    )
    done = _RUN(
        [NODE, "-e", program],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result: list[Any] = json.loads(done.stdout)
    return result


def tags(nodes: list[Any]) -> list[str]:
    return [n["tag"] for n in nodes if isinstance(n, dict)]


def test_markdown_is_part_of_the_served_script() -> None:
    assert "function setMarkdown" in SCRIPT


def test_headings_paragraphs_and_inline_marks() -> None:
    blocks = parse("# Title\n\nsome **bold**, *em*, `code` and ~~gone~~")
    assert blocks[0] == {"tag": "h1", "children": ["Title"]}
    para = blocks[1]
    assert para["tag"] == "p"
    assert tags(para["children"]) == ["strong", "em", "code", "del"]


def test_single_newlines_survive_as_line_breaks() -> None:
    (para,) = parse("line one\nline two")
    assert para["children"] == ["line one", {"tag": "br"}, "line two"]


def test_fenced_code_is_verbatim_and_closes_at_eof_mid_stream() -> None:
    (pre,) = parse("```py\nx = **not bold**\n")
    assert pre == {
        "tag": "pre",
        "children": [{"tag": "code", "children": ["x = **not bold**"]}],
    }


def test_numbered_list_split_by_blank_lines_stays_one_list() -> None:
    (lst,) = parse("3. a\n\n4. b\n5. c")
    assert lst["tag"] == "ol" and lst["start"] == 3
    assert len(lst["children"]) == 3


def test_bullets_quote_rule_and_table() -> None:
    blocks = parse("- a\n- b\n\n> quoted\n\n---\n\n| k | v |\n|---|---|\n| x | 1 |")
    assert tags(blocks) == ["ul", "blockquote", "hr", "table"]
    tbody = blocks[3]["children"][1]
    assert tbody["children"][0]["children"][1] == {"tag": "td", "children": ["1"]}


def test_snake_case_is_not_italicised() -> None:
    (para,) = parse("call read_file_now please")
    assert para["children"] == ["call read_file_now please"]


def test_html_stays_text_and_unsafe_links_are_not_linked() -> None:
    (para,) = parse('<img src=x onerror=alert(1)> [x](javascript:alert(1))')
    assert all(isinstance(c, str) for c in para["children"])
    assert "".join(para["children"]).startswith("<img src=x")


def test_safe_link_keeps_its_target() -> None:
    (para,) = parse("see [docs](https://example.com/a)")
    link = para["children"][1]
    assert link == {"tag": "a", "href": "https://example.com/a", "children": ["docs"]}
