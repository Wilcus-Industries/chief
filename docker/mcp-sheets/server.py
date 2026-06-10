"""Google Sheets MCP server — chief's own FastMCP implementation.

Replaces the ``xing5/mcp-google-sheets`` wrapper with a first-party server
that follows the same multi-account pattern as ``mcp-calendar`` (issue #47):

Auth — multi-account (issue #47, per-request re-scan issue #60):
    Re-scans ``TOKEN_DIR`` (default ``/token``) on every request so a token
    dropped after startup is picked up immediately — no restart needed.  On each
    ``tools/call``, ``_get_services()`` reads ``X-Account-Label`` from the
    Starlette request via FastMCP's per-call ``request_context`` — the same
    technique calendar uses (#56 fix).  Falls back to the first (default)
    credential when no header is present.

Refresh write-back — per-account atomic (the write-race fix, issue #47):
    After a credential is refreshed, the updated token is written to the
    *same file it was loaded from* using a temp-then-rename idiom so readers
    never see a partial write.  Concurrent refreshes on *different* account
    files are safe: each targets its own path and rename is atomic at the OS
    level.  Concurrent refreshes on the *same* file are serialised by the OS
    rename (last writer wins, never corrupt).

Row-1 guard:
    ``row1_guard.py`` (local to this image) refuses any edit that touches
    row 1, server-side.  We hook it into every write-tool call directly.

Tool names are lower_snake to match the ``mcp__sheets__*`` names in
``src/chief/tools/sheets/mcp.py``.  Standalone image — imports nothing from
the chief package.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP
from row1_guard import _check_row_1, blocked_result
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("sheets.server")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
#: Legacy single-token path (backward compat).
TOKEN_PATH = os.environ.get("TOKEN_PATH", "/token/google_token.json")
#: Directory scanned for all ``google_token*.json`` files.
TOKEN_DIR = os.environ.get("TOKEN_DIR", str(Path(TOKEN_PATH).parent))
PORT = int(os.environ.get("PORT", "8002"))

#: Header name chief stamps with the thread's active account label.
_ACCOUNT_HEADER = "x-account-label"

# ---------------------------------------------------------------------------
# Multi-account credential registry
# ---------------------------------------------------------------------------

_TOKEN_PREFIX = "google_token"
_LEGACY_NAME = "google_token.json"


def _token_path_for_label(token_dir: Path, label: str, path: Path) -> Path:
    """Return the path where ``label``'s credential was loaded from."""
    return path


def _load_credentials(token_dir: Path) -> dict[str, tuple[Any, Path]]:
    """Scan ``token_dir`` for ``google_token*.json`` files.

    Returns a label → (Credentials, token_path) dict.  The legacy
    ``google_token.json`` is always the first entry.
    """
    if not token_dir.is_dir():
        return {}
    entries: dict[str, tuple[Any, Path]] = {}
    legacy: list[tuple[str, Any, Path]] = []
    labeled: list[tuple[str, Any, Path]] = []
    for path in sorted(token_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.name.startswith(_TOKEN_PREFIX):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            scopes = raw.get("scopes") or SCOPES
            creds = Credentials.from_authorized_user_info(raw, scopes=scopes)
            label: str | None = raw.get("account")
            if not label:
                stem = path.stem
                prefix = _TOKEN_PREFIX + "_"
                label = stem[len(prefix):] if stem.startswith(prefix) else stem
            if path.name == _LEGACY_NAME:
                legacy.append((label, creds, path))
            else:
                labeled.append((label, creds, path))
        except Exception:  # noqa: BLE001
            log.warning("failed to load token %s", path, exc_info=True)
    for label, creds, path in legacy + labeled:
        entries[label] = (creds, path)
    return entries


# ---------------------------------------------------------------------------
# Per-account atomic token write-back
# ---------------------------------------------------------------------------


def _save_token(creds: Any, token_path: Path) -> None:
    """Atomically write ``creds`` back to ``token_path``.

    Uses write-to-temp-then-rename so readers never see a partial write.
    The temp file is created in the same directory to guarantee
    intra-filesystem rename (atomic on POSIX).
    """
    try:
        data = json.loads(creds.to_json())
        text = json.dumps(data, ensure_ascii=False)
        parent = token_path.parent
        fd, tmp_name = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
            Path(tmp_name).replace(token_path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except Exception:  # noqa: BLE001
        log.warning("failed to write token %s", token_path, exc_info=True)


def _refresh_if_needed(creds: Any, token_path: Path) -> Any:
    """Refresh ``creds`` in-memory if expired, then write back atomically."""
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_path)
        except Exception:  # noqa: BLE001
            log.warning("token refresh failed for %s", token_path, exc_info=True)
    return creds


# ---------------------------------------------------------------------------
# Per-request credential/service selection (dynamic re-scan, issue #60)
# ---------------------------------------------------------------------------

# No boot-time registry build: we re-scan TOKEN_DIR on every request so a token
# dropped after the server starts is picked up immediately without a restart.
# For a handful of token files the directory scan is negligible.

_token_dir = Path(TOKEN_DIR)

log.info("Sheets MCP ready | TOKEN_DIR=%s", TOKEN_DIR)


def _build_service_registry() -> tuple[dict[str, tuple[Any, Any]], tuple[Any, Any] | None]:
    """Scan TOKEN_DIR and build a label → (sheets_svc, drive_svc) dict.

    Returns ``(registry, default_pair)`` where ``default_pair`` is the first
    entry or ``None`` when the dir is empty.

    Falls back to the legacy single-file TOKEN_PATH when the directory scan
    yields nothing — preserves backward compat for single-token deploys.

    Refreshes credentials in-memory if expired and writes back atomically using
    ``_refresh_if_needed`` so the per-account token file stays current without
    a write race.
    """
    creds_registry = _load_credentials(_token_dir)

    if not creds_registry:
        # Legacy fallback: try the single TOKEN_PATH.
        try:
            _legacy_path = Path(TOKEN_PATH)
            _legacy_creds = Credentials.from_authorized_user_file(
                TOKEN_PATH, scopes=SCOPES
            )
            if _legacy_creds.expired and _legacy_creds.refresh_token:
                _legacy_creds.refresh(Request())
            creds_registry["_legacy"] = (_legacy_creds, _legacy_path)
        except Exception:  # noqa: BLE001
            pass  # No credentials at all; tools will error on the actual call.

    registry: dict[str, tuple[Any, Any]] = {}
    for lbl, (creds, token_path) in creds_registry.items():
        try:
            creds = _refresh_if_needed(creds, token_path)
            sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
            drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            registry[lbl] = (sheets_svc, drive_svc)
        except Exception:  # noqa: BLE001
            log.warning("failed to build sheets services for %r", lbl, exc_info=True)

    default = next(iter(registry.values()), None)
    return registry, default


def _get_services_for_label(
    label: str | None,
) -> tuple[Any, Any] | tuple[None, None]:
    """Return (sheets_svc, drive_svc) for ``label``, re-scanning TOKEN_DIR each call.

    A token dropped after module load is discovered here because we call
    ``_build_service_registry()`` on every invocation.  Falls back to the
    default (first) service pair when ``label`` is absent or not in the registry.
    """
    registry, default = _build_service_registry()
    if label and label in registry:
        return registry[label]
    if default is not None:
        return default
    return None, None


mcp = FastMCP("sheets", host="0.0.0.0", port=PORT)


def _get_services() -> tuple[Any, Any] | tuple[None, None]:
    """Return (sheets_service, drive_service) for the current request.

    Reads ``X-Account-Label`` from the per-call Starlette request via
    FastMCP's ``request_context``.  Falls back to the default (first loaded)
    services.  See mcp-calendar's ``_get_service()`` for the full explanation
    of why ContextVar is NOT used here.

    Re-scans TOKEN_DIR on every call (issue #60) so tokens dropped at runtime
    are picked up without a restart.
    """
    label: str | None = None
    try:
        req = mcp.get_context().request_context.request
        if req is not None:
            label = req.headers.get(_ACCOUNT_HEADER)
    except LookupError:
        pass
    return _get_services_for_label(label)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _column_index_to_letter(index: int) -> str:
    """Convert 0-based column index to A1 letter (0='A', 25='Z', 26='AA', …)."""
    result = ""
    while index >= 0:
        result = chr(index % 26 + ord("A")) + result
        index = index // 26 - 1
    return result


def _letter_to_column_index(letter: str) -> int:
    """Convert A1 letter to 0-based column index ('A'=0, 'Z'=25, 'AA'=26, …)."""
    result = 0
    for char in letter.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def _parse_a1_notation(range_str: str) -> dict[str, int]:
    """Parse A1 notation range to row/column indices (0-based, exclusive end)."""
    match = re.match(r"^([A-Z]+)?(\d+)?(?::([A-Z]+)?(\d+)?)?$", range_str.upper())
    if not match:
        raise ValueError(f"Invalid A1 notation: {range_str}")
    start_col, start_row, end_col, end_row = match.groups()
    result: dict[str, int] = {}
    if start_col:
        result["startColumnIndex"] = _letter_to_column_index(start_col)
    if start_row:
        result["startRowIndex"] = int(start_row) - 1
    if end_col:
        result["endColumnIndex"] = _letter_to_column_index(end_col) + 1
    elif start_col:
        result["endColumnIndex"] = result["startColumnIndex"] + 1
    if end_row:
        result["endRowIndex"] = int(end_row)
    elif start_row:
        result["endRowIndex"] = result["startRowIndex"] + 1
    return result


def _get_sheet_id(sheets_service: Any, spreadsheet_id: str, sheet_name: str) -> int | None:
    """Return the numeric sheetId for ``sheet_name``, or None if not found."""
    try:
        sp = sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,sheetId))",
        ).execute()
        for sheet in sp.get("sheets", []):
            if sheet["properties"]["title"] == sheet_name:
                return sheet["properties"]["sheetId"]
        return None
    except Exception:  # noqa: BLE001
        return None


def _split_chart_source_ranges(
    source_range: dict[str, int],
) -> tuple[dict[str, int], list[dict[str, int]]]:
    """Split a chart source range into a domain range and series ranges."""
    start_col = source_range.get("startColumnIndex")
    end_col = source_range.get("endColumnIndex")
    if start_col is None or end_col is None or end_col - start_col <= 1:
        return source_range, [source_range]
    domain_range = {**source_range, "endColumnIndex": start_col + 1}
    series_ranges = [
        {**source_range, "startColumnIndex": col, "endColumnIndex": col + 1}
        for col in range(start_col + 1, end_col)
    ]
    return domain_range, series_ranges


def _guard(name: str, arguments: dict[str, Any]) -> Any | None:
    """Return a blocked_result if the write touches row 1, else None."""
    err = _check_row_1(name, arguments)
    return blocked_result(err) if err else None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_sheet_data(
    spreadsheet_id: str,
    sheet: str,
    range: Optional[str] = None,
    include_grid_data: bool = False,
) -> dict[str, Any]:
    """Get data from a specific sheet in a Google Spreadsheet."""
    sheets_service, _ = _get_services()
    full_range = f"{sheet}!{range}" if range else sheet

    def _call() -> dict[str, Any]:
        if include_grid_data:
            return sheets_service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                ranges=[full_range],
                includeGridData=True,
            ).execute()
        values_result = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=full_range
        ).execute()
        return {
            "spreadsheetId": spreadsheet_id,
            "valueRanges": [
                {"range": full_range, "values": values_result.get("values", [])}
            ],
        }

    return await asyncio.to_thread(_call)


@mcp.tool()
async def get_sheet_formulas(
    spreadsheet_id: str,
    sheet: str,
    range: Optional[str] = None,
) -> list[list[Any]]:
    """Get formulas from a specific sheet in a Google Spreadsheet."""
    sheets_service, _ = _get_services()
    full_range = f"{sheet}!{range}" if range else sheet

    def _call() -> list[list[Any]]:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=full_range,
            valueRenderOption="FORMULA",
        ).execute()
        return result.get("values", [])

    return await asyncio.to_thread(_call)


@mcp.tool()
async def update_cells(
    spreadsheet_id: str,
    sheet: str,
    range: str,
    data: list[list[Any]],
) -> Any:
    """Update cells in a Google Spreadsheet."""
    blocked = _guard("update_cells", {"range": range})
    if blocked:
        return blocked
    sheets_service, _ = _get_services()
    full_range = f"{sheet}!{range}"

    def _call() -> dict[str, Any]:
        return sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=full_range,
            valueInputOption="USER_ENTERED",
            body={"values": data},
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def batch_update_cells(
    spreadsheet_id: str,
    sheet: str,
    ranges: dict[str, list[list[Any]]],
) -> Any:
    """Batch update multiple ranges in a Google Spreadsheet."""
    blocked = _guard("batch_update_cells", {"ranges": ranges})
    if blocked:
        return blocked
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        data = [
            {"range": f"{sheet}!{r}", "values": v} for r, v in ranges.items()
        ]
        return sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def add_rows(
    spreadsheet_id: str,
    sheet: str,
    count: int,
    start_row: Optional[int] = None,
) -> Any:
    """Add rows to a sheet in a Google Spreadsheet."""
    blocked = _guard("add_rows", {"start_row": start_row})
    if blocked:
        return blocked
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        sp = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_id = None
        for s in sp["sheets"]:
            if s["properties"]["title"] == sheet:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return {"error": f"Sheet '{sheet}' not found"}
        start = start_row if start_row is not None else 0
        return sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start,
                            "endIndex": start + count,
                        },
                        "inheritFromBefore": start_row is not None and start_row > 0,
                    }
                }]
            },
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def add_columns(
    spreadsheet_id: str,
    sheet: str,
    count: int,
    start_column: Optional[int] = None,
) -> dict[str, Any]:
    """Add columns to a sheet in a Google Spreadsheet."""
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        sp = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_id = None
        for s in sp["sheets"]:
            if s["properties"]["title"] == sheet:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return {"error": f"Sheet '{sheet}' not found"}
        start = start_column if start_column is not None else 0
        return sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": start,
                            "endIndex": start + count,
                        },
                        "inheritFromBefore": start_column is not None and start_column > 0,
                    }
                }]
            },
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def list_sheets(spreadsheet_id: str) -> list[str]:
    """List all sheets in a Google Spreadsheet."""
    sheets_service, _ = _get_services()

    def _call() -> list[str]:
        sp = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        return [s["properties"]["title"] for s in sp["sheets"]]

    return await asyncio.to_thread(_call)


@mcp.tool()
async def copy_sheet(
    src_spreadsheet: str,
    src_sheet: str,
    dst_spreadsheet: str,
    dst_sheet: str,
) -> dict[str, Any]:
    """Copy a sheet from one spreadsheet to another."""
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        src = sheets_service.spreadsheets().get(spreadsheetId=src_spreadsheet).execute()
        src_sheet_id = None
        for s in src["sheets"]:
            if s["properties"]["title"] == src_sheet:
                src_sheet_id = s["properties"]["sheetId"]
                break
        if src_sheet_id is None:
            return {"error": f"Source sheet '{src_sheet}' not found"}
        copy_result = sheets_service.spreadsheets().sheets().copyTo(
            spreadsheetId=src_spreadsheet,
            sheetId=src_sheet_id,
            body={"destinationSpreadsheetId": dst_spreadsheet},
        ).execute()
        if "title" in copy_result and copy_result["title"] != dst_sheet:
            copy_sheet_id = copy_result["sheetId"]
            rename_result = sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=dst_spreadsheet,
                body={
                    "requests": [{
                        "updateSheetProperties": {
                            "properties": {"sheetId": copy_sheet_id, "title": dst_sheet},
                            "fields": "title",
                        }
                    }]
                },
            ).execute()
            return {"copy": copy_result, "rename": rename_result}
        return {"copy": copy_result}

    return await asyncio.to_thread(_call)


@mcp.tool()
async def rename_sheet(
    spreadsheet: str,
    sheet: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename a sheet in a Google Spreadsheet."""
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        sp = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet).execute()
        sheet_id = None
        for s in sp["sheets"]:
            if s["properties"]["title"] == sheet:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return {"error": f"Sheet '{sheet}' not found"}
        return sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet,
            body={
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "title": new_name},
                        "fields": "title",
                    }
                }]
            },
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def get_multiple_sheet_data(
    queries: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Get data from multiple specific ranges in Google Spreadsheets."""
    sheets_service, _ = _get_services()

    def _call() -> list[dict[str, Any]]:
        results = []
        for query in queries:
            spreadsheet_id = query.get("spreadsheet_id")
            sheet = query.get("sheet")
            range_str = query.get("range")
            if not all([spreadsheet_id, sheet, range_str]):
                results.append({**query, "error": "Missing required keys"})
                continue
            try:
                full_range = f"{sheet}!{range_str}"
                result = sheets_service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=full_range
                ).execute()
                results.append({**query, "data": result.get("values", [])})
            except Exception as e:  # noqa: BLE001
                results.append({**query, "error": str(e)})
        return results

    return await asyncio.to_thread(_call)


@mcp.tool()
async def get_multiple_spreadsheet_summary(
    spreadsheet_ids: list[str],
    rows_to_fetch: int = 5,
) -> list[dict[str, Any]]:
    """Get a summary (sheet names, headers, first rows) of multiple spreadsheets."""
    sheets_service, _ = _get_services()

    def _call() -> list[dict[str, Any]]:
        summaries = []
        for spreadsheet_id in spreadsheet_ids:
            summary: dict[str, Any] = {
                "spreadsheet_id": spreadsheet_id,
                "title": None,
                "sheets": [],
                "error": None,
            }
            try:
                sp = sheets_service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    fields="properties.title,sheets(properties(title,sheetId))",
                ).execute()
                summary["title"] = sp.get("properties", {}).get("title", "Unknown Title")
                sheet_summaries = []
                for sheet in sp.get("sheets", []):
                    title = sheet.get("properties", {}).get("title")
                    sheet_id = sheet.get("properties", {}).get("sheetId")
                    s_summary: dict[str, Any] = {
                        "title": title,
                        "sheet_id": sheet_id,
                        "headers": [],
                        "first_rows": [],
                        "error": None,
                    }
                    if not title:
                        s_summary["error"] = "Sheet title not found"
                        sheet_summaries.append(s_summary)
                        continue
                    try:
                        max_row = max(1, rows_to_fetch)
                        rng = f"{title}!A1:{max_row}"
                        values = sheets_service.spreadsheets().values().get(
                            spreadsheetId=spreadsheet_id, range=rng
                        ).execute().get("values", [])
                        if values:
                            s_summary["headers"] = values[0]
                            if len(values) > 1:
                                s_summary["first_rows"] = values[1:max_row]
                    except Exception as e:  # noqa: BLE001
                        s_summary["error"] = str(e)
                    sheet_summaries.append(s_summary)
                summary["sheets"] = sheet_summaries
            except Exception as e:  # noqa: BLE001
                summary["error"] = str(e)
            summaries.append(summary)
        return summaries

    return await asyncio.to_thread(_call)


@mcp.tool()
async def list_spreadsheets(
    folder_id: Optional[str] = None,
) -> list[dict[str, str]]:
    """List all spreadsheets in a Google Drive folder (or My Drive)."""
    _, drive_service = _get_services()

    def _call() -> list[dict[str, str]]:
        query = "mimeType='application/vnd.google-apps.spreadsheet'"
        if folder_id:
            query += f" and '{folder_id}' in parents"
        results = drive_service.files().list(
            q=query,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name)",
            orderBy="modifiedTime desc",
        ).execute()
        return [{"id": f["id"], "title": f["name"]} for f in results.get("files", [])]

    return await asyncio.to_thread(_call)


@mcp.tool()
async def list_folders(
    parent_folder_id: Optional[str] = None,
) -> list[dict[str, str]]:
    """List folders in a Google Drive location."""
    _, drive_service = _get_services()

    def _call() -> list[dict[str, str]]:
        query = "mimeType='application/vnd.google-apps.folder'"
        if parent_folder_id:
            query += f" and '{parent_folder_id}' in parents"
        else:
            query += " and 'root' in parents"
        results = drive_service.files().list(
            q=query,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name, parents)",
            orderBy="name",
        ).execute()
        return [
            {
                "id": f["id"],
                "name": f["name"],
                "parent": f.get("parents", ["root"])[0] if f.get("parents") else "root",
            }
            for f in results.get("files", [])
        ]

    return await asyncio.to_thread(_call)


@mcp.tool()
async def search_spreadsheets(
    query: str,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Search for spreadsheets in Google Drive by name or content."""
    _, drive_service = _get_services()
    max_results = min(max(1, max_results), 100)

    def _call() -> list[dict[str, Any]]:
        search_query = (
            f"mimeType='application/vnd.google-apps.spreadsheet' and "
            f"(name contains '{query}' or fullText contains '{query}')"
        )
        try:
            results = drive_service.files().list(
                q=search_query,
                pageSize=max_results,
                spaces="drive",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="files(id, name, createdTime, modifiedTime, owners, webViewLink)",
                orderBy="modifiedTime desc",
            ).execute()
            return [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "created_time": f.get("createdTime"),
                    "modified_time": f.get("modifiedTime"),
                    "owners": [
                        o.get("emailAddress") for o in f.get("owners", [])
                    ],
                    "web_link": f.get("webViewLink"),
                }
                for f in results.get("files", [])
            ]
        except Exception as e:  # noqa: BLE001
            return [{"error": f"Search failed: {e}"}]

    return await asyncio.to_thread(_call)


@mcp.tool()
async def find_in_spreadsheet(
    spreadsheet_id: str,
    query: str,
    sheet: Optional[str] = None,
    case_sensitive: bool = False,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Find cells containing a specific value in a Google Spreadsheet."""
    sheets_service, _ = _get_services()

    def _call() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            sp = sheets_service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(title,sheetId))",
            ).execute()
            sheets_to_search = [
                s["properties"]["title"]
                for s in sp.get("sheets", [])
                if sheet is None or s["properties"]["title"] == sheet
            ]
            if not sheets_to_search:
                return [{"error": f"Sheet '{sheet}' not found"}]
            search_q = query if case_sensitive else query.lower()
            for sheet_name in sheets_to_search:
                if len(results) >= max_results:
                    break
                response = sheets_service.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id, range=sheet_name
                ).execute()
                for row_idx, row in enumerate(response.get("values", [])):
                    if len(results) >= max_results:
                        break
                    for col_idx, cell_value in enumerate(row):
                        if len(results) >= max_results:
                            break
                        compare = str(cell_value) if case_sensitive else str(cell_value).lower()
                        if search_q in compare:
                            results.append({
                                "sheet": sheet_name,
                                "cell": f"{_column_index_to_letter(col_idx)}{row_idx + 1}",
                                "value": cell_value,
                            })
        except Exception as e:  # noqa: BLE001
            return [{"error": f"Search failed: {e}"}]
        return results

    return await asyncio.to_thread(_call)


@mcp.tool()
async def create_spreadsheet(
    title: str,
    folder_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new Google Spreadsheet."""
    _, drive_service = _get_services()

    def _call() -> dict[str, Any]:
        file_body: dict[str, Any] = {
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        if folder_id:
            file_body["parents"] = [folder_id]
        spreadsheet = drive_service.files().create(
            supportsAllDrives=True, body=file_body, fields="id, name, parents"
        ).execute()
        spreadsheet_id = spreadsheet.get("id")
        parents = spreadsheet.get("parents")
        log.info("Spreadsheet created: id=%s", spreadsheet_id)
        return {
            "spreadsheetId": spreadsheet_id,
            "title": spreadsheet.get("name", title),
            "folder": parents[0] if parents else "root",
        }

    return await asyncio.to_thread(_call)


@mcp.tool()
async def create_sheet(
    spreadsheet_id: str,
    title: str,
) -> dict[str, Any]:
    """Create a new sheet tab in an existing Google Spreadsheet."""
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        result = sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        new_sheet_props = result["replies"][0]["addSheet"]["properties"]
        return {
            "sheetId": new_sheet_props["sheetId"],
            "title": new_sheet_props["title"],
            "index": new_sheet_props.get("index"),
            "spreadsheetId": spreadsheet_id,
        }

    return await asyncio.to_thread(_call)


@mcp.tool()
async def share_spreadsheet(
    spreadsheet_id: str,
    recipients: list[dict[str, str]],
    send_notification: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Share a Google Spreadsheet with multiple users."""
    _, drive_service = _get_services()

    def _call() -> dict[str, list[dict[str, Any]]]:
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for recipient in recipients:
            email_address = recipient.get("email_address")
            role = recipient.get("role", "writer")
            if not email_address:
                failures.append({"email_address": None, "error": "Missing email_address"})
                continue
            if role not in ["reader", "commenter", "writer"]:
                failures.append({
                    "email_address": email_address,
                    "error": f"Invalid role '{role}'",
                })
                continue
            try:
                result = drive_service.permissions().create(
                    fileId=spreadsheet_id,
                    body={"type": "user", "role": role, "emailAddress": email_address},
                    sendNotificationEmail=send_notification,
                    fields="id",
                ).execute()
                successes.append({
                    "email_address": email_address,
                    "role": role,
                    "permissionId": result.get("id"),
                })
            except Exception as e:  # noqa: BLE001
                failures.append({"email_address": email_address, "error": str(e)})
        return {"successes": successes, "failures": failures}

    return await asyncio.to_thread(_call)


@mcp.tool()
async def batch_update(
    spreadsheet_id: str,
    requests: list[dict[str, Any]],
) -> Any:
    """Execute a batch update on a Google Spreadsheet."""
    blocked = _guard("batch_update", {"requests": requests})
    if blocked:
        return blocked
    sheets_service, _ = _get_services()

    def _call() -> dict[str, Any]:
        if not requests:
            return {"error": "requests list cannot be empty"}
        if not all(isinstance(req, dict) for req in requests):
            return {"error": "Each request must be a dictionary"}
        return sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()

    return await asyncio.to_thread(_call)


@mcp.tool()
async def add_chart(
    spreadsheet_id: str,
    sheet: str,
    chart_type: str,
    data_range: str,
    title: Optional[str] = None,
    x_axis_label: Optional[str] = None,
    y_axis_label: Optional[str] = None,
    position_x: int = 0,
    position_y: int = 0,
    width: int = 600,
    height: int = 400,
) -> Any:
    """Add a chart to a Google Spreadsheet."""
    blocked = _guard("add_chart", {"data_range": data_range})
    if blocked:
        return blocked
    sheets_service, _ = _get_services()
    valid_chart_types = ["COLUMN", "BAR", "LINE", "AREA", "PIE", "SCATTER", "COMBO", "HISTOGRAM"]
    if chart_type.upper() not in valid_chart_types:
        return {"error": f"Invalid chart type '{chart_type}'."}
    chart_type = chart_type.upper()

    def _call() -> dict[str, Any]:
        sheet_id = _get_sheet_id(sheets_service, spreadsheet_id, sheet)
        if sheet_id is None:
            return {"error": f"Sheet '{sheet}' not found in spreadsheet"}
        try:
            range_indices = _parse_a1_notation(data_range)
        except ValueError as e:
            return {"error": str(e)}
        source_range = {"sheetId": sheet_id, **range_indices}
        domain_range, series_ranges = _split_chart_source_ranges(source_range)
        if chart_type == "PIE":
            chart_spec: dict[str, Any] = {
                "pieChart": {
                    "legendPosition": "RIGHT_LEGEND",
                    "domain": {"sourceRange": {"sources": [domain_range]}},
                    "series": {"sourceRange": {"sources": [series_ranges[0]]}},
                }
            }
        else:
            chart_spec = {
                "basicChart": {
                    "chartType": chart_type,
                    "legendPosition": "RIGHT_LEGEND",
                    "axis": [{"position": "BOTTOM_AXIS"}],
                    "domains": [{"domain": {"sourceRange": {"sources": [domain_range]}}}],
                    "series": [
                        {"series": {"sourceRange": {"sources": [sr]}}, "targetAxis": "LEFT_AXIS"}
                        for sr in series_ranges
                    ],
                    "headerCount": 1,
                }
            }
            if x_axis_label:
                chart_spec["basicChart"]["axis"][0]["title"] = x_axis_label
            if y_axis_label:
                chart_spec["basicChart"]["axis"].append(
                    {"position": "LEFT_AXIS", "title": y_axis_label}
                )
            else:
                chart_spec["basicChart"]["axis"].append({"position": "LEFT_AXIS"})
        if title:
            chart_spec["title"] = title
        try:
            result = sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [{
                        "addChart": {
                            "chart": {
                                "spec": chart_spec,
                                "position": {
                                    "overlayPosition": {
                                        "anchorCell": {
                                            "sheetId": sheet_id,
                                            "rowIndex": 0,
                                            "columnIndex": 0,
                                        },
                                        "offsetXPixels": position_x,
                                        "offsetYPixels": position_y,
                                        "widthPixels": width,
                                        "heightPixels": height,
                                    }
                                },
                            }
                        }
                    }]
                },
            ).execute()
            return {
                "success": True,
                "message": f"Chart '{title or chart_type}' added successfully",
                "chartId": (
                    result.get("replies", [{}])[0]
                    .get("addChart", {})
                    .get("chart", {})
                    .get("chartId")
                ),
                "result": result,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"Failed to add chart: {e}"}

    return await asyncio.to_thread(_call)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: StarletteRequest) -> JSONResponse:
    """Liveness probe for the compose healthcheck."""
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import anyio
    import uvicorn

    async def _serve() -> None:
        starlette_app = mcp.streamable_http_app()
        config = uvicorn.Config(
            starlette_app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()

    anyio.run(_serve)
