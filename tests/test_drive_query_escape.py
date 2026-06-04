"""Coverage for the Drive query-literal escaper.

``docker/mcp-drive/drive_query.py`` lives in a vendored image (excluded from the package
and the venv), so we load it by path — ``server.py`` itself can't be imported (its
module-load Google auth + heavy deps aren't in the venv). The escaper is security-
critical: it stops a model-supplied file name from breaking out of the single-quoted
``q=`` literal, so a name like ``it's.pdf`` can't alter the Drive query. Backslash must
be escaped before the quote, else the quote's own escape backslash gets doubled.
"""

import importlib.util
from pathlib import Path

import pytest

_QUERY_PATH = (
    Path(__file__).resolve().parents[1] / "docker" / "mcp-drive" / "drive_query.py"
)
_spec = importlib.util.spec_from_file_location("drive_query", _QUERY_PATH)
assert _spec and _spec.loader
drive_query = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_query)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("it's.pdf", "it\\'s.pdf"),  # single quote escaped → can't close the literal
        ("a\\b", "a\\\\b"),  # backslash doubled
        ("a\\'b", "a\\\\\\'b"),  # backslash-first ordering: \ then ' (not \\')
        ("clean-name.pdf", "clean-name.pdf"),  # nothing to escape → unchanged
        ("folder_id_123", "folder_id_123"),  # clean folder id → unchanged
        ("", ""),  # empty → empty
    ],
)
def test_escape_drive_query(value: str, expected: str) -> None:
    assert drive_query._escape_drive_query(value) == expected
