"""Row-1 (header) write guard for the Google Sheets MCP server.

Pure stdlib (``re`` only) so it is importable by a unit test outside the image — the
``docker/`` tree is excluded from chief's suite and the image deps aren't in the venv,
yet this predicate is security-critical (the personas prompt promises header writes are
blocked server-side), so it must be testable. ``server.py`` imports from here and wires
the monkeypatch.

``_a1_includes_row_1`` collects *every* row number across both ``:``-separated endpoints
and blocks when the smallest is row 1. An earlier version inspected only the start row, so
a reversed range whose end is row 1 (``A2:C1``, ``B3:D1``) slipped through — Google
normalizes the rectangle and writes the header anyway. Column-only endpoints (``A``,
``A:B``) and anything unparseable fail safe (blocked).
"""

import re
from typing import Any

ROW_1_ERROR = (
    "You cannot edit the first row of a Google Sheet. "
    "Ask a human to perform this edit for you if it was intentional."
)


def _a1_includes_row_1(range_str: str) -> bool:
    """True if the A1 range touches row 1 (or can't be proven not to)."""
    s = range_str.split("!")[-1].strip().upper()
    if not s:
        return True  # empty / sheet-only reference → whole sheet, includes row 1
    rows: list[int] = []
    for part in s.split(":"):
        cell = re.match(r"^[A-Z]*(\d+)$", part)
        if cell:
            rows.append(int(cell.group(1)))
        elif re.match(r"^[A-Z]+$", part):
            return True  # column-only endpoint (A, B) → unbounded rows, includes row 1
        else:
            return True  # unrecognized token → fail safe (block)
    if not rows:
        return True
    return min(rows) <= 1


def _check_row_1(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Return ROW_1_ERROR if this write touches the header row, else None."""
    if tool_name == "update_cells":
        r = arguments.get("range", "")
        if r and _a1_includes_row_1(r):
            return ROW_1_ERROR

    elif tool_name == "batch_update_cells":
        ranges = arguments.get("ranges", {})
        if any(_a1_includes_row_1(k) for k in ranges):
            return ROW_1_ERROR

    elif tool_name == "add_rows":
        start = arguments.get("start_row")
        if start is None or start == 0:
            return ROW_1_ERROR

    elif tool_name == "add_chart":
        r = arguments.get("data_range", "")
        if r and _a1_includes_row_1(r):
            return ROW_1_ERROR

    elif tool_name == "batch_update":
        for req in arguments.get("requests", []):
            uc = req.get("updateCells", {})
            if uc.get("range", {}).get("startRowIndex", -1) == 0:
                return ROW_1_ERROR
            ins = req.get("insertDimension", {})
            rng = ins.get("range", {})
            if rng.get("dimension") == "ROWS" and rng.get("startIndex", -1) == 0:
                return ROW_1_ERROR

    return None
