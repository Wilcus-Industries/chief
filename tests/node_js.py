"""Run the web client's pure JS under node, for tests. Skip where node is absent:
mark a module with ``pytestmark = needs_node``."""

import json
import shutil
import subprocess
from typing import Any

import pytest

NODE = shutil.which("node")
# Bound at import: a fake subprocess.run from another test file can outlive its
# test in a full-suite run, and this must reach the real node.
_RUN = subprocess.run
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(program: str, stdin: str | None = None) -> Any:
    """Run ``program``; it writes one JSON value to stdout, returned parsed."""
    assert NODE is not None
    done = _RUN(
        [NODE, "-e", program],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


def check_js(path: str) -> None:
    """Syntax-check a script file (``node --check``)."""
    assert NODE is not None
    _RUN([NODE, "--check", path], check=True, timeout=30)
