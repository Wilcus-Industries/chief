"""Regression coverage for the Sheets header-row write guard.

``docker/mcp-sheets/row1_guard.py`` lives in a vendored image (excluded from the package
and the venv), so we load it by path. The predicate is security-critical — the personas
prompt promises row-1 writes are blocked server-side — and an earlier version inspected
only a range's start row, letting a reversed range whose *end* is row 1 (``A2:C1``,
``B3:D1``) overwrite the header. These cases lock that bypass shut.
"""

import importlib.util
from pathlib import Path

import pytest

_GUARD_PATH = (
    Path(__file__).resolve().parents[1] / "docker" / "mcp-sheets" / "row1_guard.py"
)
_spec = importlib.util.spec_from_file_location("row1_guard", _GUARD_PATH)
assert _spec and _spec.loader
row1_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(row1_guard)


@pytest.mark.parametrize(
    ("range_str", "blocked"),
    [
        ("A1:B2", True),  # starts at row 1
        ("A2:B3", False),  # rows 2-3, header untouched
        ("A2:C1", True),  # reversed: end is row 1 (the old bypass)
        ("B3:D1", True),  # reversed: end is row 1 (the old bypass)
        ("1:5", True),  # whole-row 1..5
        ("2:5", False),  # whole-row 2..5
        ("A:B", True),  # column-only → unbounded rows, includes row 1
        ("A5", False),  # single cell, row 5
        ("Sheet1!A2:C1", True),  # sheet-qualified reversed range still blocked
        ("Sheet1!B2:D9", False),  # sheet-qualified, header untouched
        ("", True),  # empty / whole sheet → fail safe
        ("garbage", True),  # unparseable → fail safe
    ],
)
def test_a1_includes_row_1(range_str: str, blocked: bool) -> None:
    assert row1_guard._a1_includes_row_1(range_str) is blocked


def test_check_update_cells() -> None:
    assert row1_guard._check_row_1("update_cells", {"range": "A1:B2"})
    assert row1_guard._check_row_1("update_cells", {"range": "A2:C1"})  # reversed
    assert row1_guard._check_row_1("update_cells", {"range": "A2:B3"}) is None


def test_check_batch_update_cells() -> None:
    assert row1_guard._check_row_1(
        "batch_update_cells", {"ranges": {"A2:B3": [], "A1:C1": []}}
    )
    assert (
        row1_guard._check_row_1("batch_update_cells", {"ranges": {"A2:B3": []}}) is None
    )


def test_check_add_rows() -> None:
    assert row1_guard._check_row_1("add_rows", {"start_row": 0})
    assert row1_guard._check_row_1("add_rows", {})  # missing → blocked
    assert row1_guard._check_row_1("add_rows", {"start_row": 5}) is None


def test_check_add_chart() -> None:
    assert row1_guard._check_row_1("add_chart", {"data_range": "A1:B10"})
    assert row1_guard._check_row_1("add_chart", {"data_range": "A2:B10"}) is None


def test_check_batch_update() -> None:
    update_header = {
        "requests": [{"updateCells": {"range": {"startRowIndex": 0}}}]
    }
    assert row1_guard._check_row_1("batch_update", update_header)

    insert_at_top = {
        "requests": [
            {"insertDimension": {"range": {"dimension": "ROWS", "startIndex": 0}}}
        ]
    }
    assert row1_guard._check_row_1("batch_update", insert_at_top)

    below_header = {
        "requests": [{"updateCells": {"range": {"startRowIndex": 5}}}]
    }
    assert row1_guard._check_row_1("batch_update", below_header) is None


def test_unguarded_tool_passes() -> None:
    assert row1_guard._check_row_1("get_sheet_data", {"range": "A1:Z99"}) is None


def test_blocked_result_shape() -> None:
    """The refusal must be a ``CallToolResult`` the lowlevel handler short-circuits on.

    A bare content list is treated as "no structured output" and, because the guarded
    tools declare an ``outputSchema``, gets replaced by a generic validation error —
    losing ROW_1_ERROR. A ``CallToolResult`` is returned verbatim, keeping the message.
    """
    from mcp.types import CallToolResult

    result = row1_guard.blocked_result(row1_guard.ROW_1_ERROR)
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.content[0].type == "text"
    assert result.content[0].text == row1_guard.ROW_1_ERROR
