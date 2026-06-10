"""Google Drive MCP server — FastMCP over streamable-HTTP (ported from genesis-x).

Two tools: ``ReadDriveFile`` (read a Doc / PDF / Office file by Drive URL) and
``UploadMarkdownAsPDF`` (render a local Markdown file to PDF and upload it to a folder).
Ported from genesis-x's ``drive/server.py`` with three changes for chief:

- transport ``sse`` → ``streamable-http`` (chief reaches it at ``/mcp``),
- a ``/health`` route for the compose healthcheck,
- multi-account credential selection via ``X-Account-Label`` request header
  (issue #47): same pattern as mcp-calendar (issue #46/#56).

Auth — multi-account (issue #47):
    The server scans TOKEN_DIR (default ``/token``) for all ``google_token*.json``
    files and loads them into a per-label credential registry at startup. On each
    ``tools/call``, ``_get_service()`` reads ``X-Account-Label`` directly from the
    Starlette request object exposed via FastMCP's per-call ``request_context``
    (``mcp.get_context().request_context.request``).  Falls back to the first
    (default) credential when no header is present — fully backward-compatible
    with single-account deploys.

    Credentials are refreshed in memory only — no write-back. Drive is a
    read/upload service; the sheets container is the sole writer of token files.

Standalone image — imports nothing from the chief package.
"""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import markdown
from drive_query import _escape_drive_query
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from weasyprint import HTML

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("drive.server")

SCOPES = ["https://www.googleapis.com/auth/drive"]
#: Legacy single-token path (backward compat). When present it is always the default.
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/token/google_token.json")
#: Directory scanned for all ``google_token*.json`` files.
TOKEN_DIR = os.environ.get("TOKEN_DIR", str(Path(TOKEN_PATH).parent))
PORT = int(os.environ.get("PORT", "8001"))

#: Header name chief stamps with the thread's active account label.
#: ASGI lower-cases incoming header names; we match the normalised form.
_ACCOUNT_HEADER = "x-account-label"

# ---------------------------------------------------------------------------
# Multi-account credential registry
# ---------------------------------------------------------------------------

_TOKEN_PREFIX = "google_token"
_LEGACY_NAME = "google_token.json"


def _load_credentials(token_dir: Path) -> dict[str, Any]:
    """Scan ``token_dir`` for ``google_token*.json`` files.

    Returns a label → Credentials dict. The legacy ``google_token.json`` (if
    present) is always the first entry (and therefore the default fallback).
    Credentials are loaded but NOT refreshed here; google-api-python-client
    refreshes in memory on the first API call.
    """
    if not token_dir.is_dir():
        return {}
    registry: dict[str, Any] = {}
    legacy: list[tuple[str, Any]] = []
    labeled: list[tuple[str, Any]] = []
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
                legacy.append((label, creds))
            else:
                labeled.append((label, creds))
        except Exception:  # noqa: BLE001
            log.warning("failed to load token %s", path, exc_info=True)
    for label, creds in legacy + labeled:
        registry[label] = creds
    return registry


# Load all registered accounts at startup.
_creds_registry: dict[str, Any] = _load_credentials(Path(TOKEN_DIR))

if _creds_registry:
    labels = list(_creds_registry)
    log.info(
        "Drive MCP loaded %d account(s): %s",
        len(_creds_registry),
        ", ".join(labels),
    )
else:
    # No token files found — fall back to the legacy single-file path for compat.
    log.warning(
        "No token files found in TOKEN_DIR=%s; trying TOKEN_PATH=%s",
        TOKEN_DIR,
        TOKEN_PATH,
    )
    try:
        _legacy_creds = Credentials.from_authorized_user_file(TOKEN_PATH, scopes=SCOPES)
        if _legacy_creds.expired and _legacy_creds.refresh_token:
            _legacy_creds.refresh(Request())
        _creds_registry["_legacy"] = _legacy_creds
        log.info("Drive MCP authenticated (legacy path) | token=%s", TOKEN_PATH)
    except Exception:
        log.exception("Failed to load any Google credentials — drive tools will error")


# ---------------------------------------------------------------------------
# Per-request credential/service selection
# ---------------------------------------------------------------------------

# Build a google-api-python-client service resource for every loaded credential.
# Each service is built once; google-api-python-client refreshes the access token
# in memory on each call, so no write-back is needed.
_service_registry: dict[str, Any] = {}
for _label, _creds in _creds_registry.items():
    try:
        _service_registry[_label] = build(
            "drive", "v3", credentials=_creds, cache_discovery=False
        )
    except Exception:  # noqa: BLE001
        log.warning("failed to build drive service for %r", _label, exc_info=True)

# The first (default) service — used when no account header is present.
_default_service: Any = next(iter(_service_registry.values()), None)

mcp = FastMCP("drive", host="0.0.0.0", port=PORT)


def _get_service() -> Any:
    """Return the drive service resource for the current request.

    Reads ``X-Account-Label`` from the per-call Starlette request exposed by
    FastMCP's ``request_context`` (set by the MCP lowlevel server *per handler
    invocation*, not per session).  Falls back to the default (first loaded)
    service when the label is absent or unknown — backward compat for
    single-account deploys and requests that carry no header.

    Why not a ContextVar?  See the calendar server's docstring for the full
    explanation.  Short version: FastMCP's stateful-mode session task is spawned
    before the current HTTP POST arrives; a middleware-set ContextVar is
    invisible to the handler task.  Reading from ``request_context.request`` is
    safe because the MCP lowlevel server sets the ContextVar inside each
    per-message task.
    """
    label: str | None = None
    try:
        req = mcp.get_context().request_context.request
        if req is not None:
            label = req.headers.get(_ACCOUNT_HEADER)
    except LookupError:
        pass
    if label and label in _service_registry:
        return _service_registry[label]
    return _default_service


# ---------------------------------------------------------------------------
# CSS / file-type helpers (unchanged from original)
# ---------------------------------------------------------------------------

_PDF_CSS = """
body {
    font-family: Georgia, serif;
    font-size: 13px;
    line-height: 1.7;
    max-width: 720px;
    margin: 48px auto;
    color: #1a1a1a;
}
h1 { font-size: 2em; margin-bottom: 0.2em; }
h2 { font-size: 1.4em; margin-top: 1.6em; }
h3 { font-size: 1.1em; margin-top: 1.2em; }
p { margin: 0.8em 0; }
a { color: #0066cc; }
code {
    font-family: monospace;
    background: #f4f4f4;
    padding: 0.1em 0.3em;
    border-radius: 3px;
    font-size: 0.9em;
}
pre code {
    display: block;
    padding: 1em;
    overflow-x: auto;
    white-space: pre-wrap;
}
blockquote {
    border-left: 3px solid #ccc;
    margin: 1em 0;
    padding-left: 1em;
    color: #555;
}
hr { border: none; border-top: 1px solid #ddd; margin: 2em 0; }
"""

_FILE_ID_PATTERNS = [
    r"/file/d/([a-zA-Z0-9_-]+)",
    r"/document/d/([a-zA-Z0-9_-]+)",
    r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
    r"[?&]id=([a-zA-Z0-9_-]+)",
]

_MIME_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "text/html": ".html",
    "text/csv": ".csv",
}


def _extract_file_id(url: str) -> str:
    for pattern in _FILE_ID_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    raise ValueError(f"Could not extract file ID from URL: {url}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ReadDriveFile(url: str) -> str:
    """Read the text of a Google Doc, PDF, or Office file (docx/xlsx/pptx) by Drive URL."""
    file_id = _extract_file_id(url)
    service = _get_service()

    def _fetch() -> str:
        meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)
        log.info("ReadDriveFile | id=%s name=%s mime=%s", file_id, name, mime)
        if mime == "application/vnd.google-apps.document":
            data = (
                service.files()
                .export(fileId=file_id, mimeType="text/plain")
                .execute()
            )
            return data.decode("utf-8") if isinstance(data, bytes) else data
        if mime.startswith("text/") and mime not in _MIME_EXT:
            data = service.files().get_media(fileId=file_id).execute()
            return data.decode("utf-8", errors="replace")
        ext = _MIME_EXT.get(mime)
        if not ext:
            raise ValueError(f"Unsupported mime type: {mime} (file: {name})")
        data = service.files().get_media(fileId=file_id).execute()
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                return MarkItDown().convert(path).text_content
            except Exception as e:
                log.warning(
                    "ReadDriveFile | markitdown failed id=%s mime=%s err=%s; "
                    "falling back to raw decode",
                    file_id,
                    mime,
                    e,
                )
                return data.decode("utf-8", errors="replace")
        finally:
            os.unlink(path)

    text = await asyncio.to_thread(_fetch)
    log.info("ReadDriveFile | id=%s chars=%d", file_id, len(text))
    return text


@mcp.tool()
async def UploadMarkdownAsPDF(file_path: str, folder_id: str) -> str:
    """Render a Markdown file as a PDF and upload it to a Drive folder. Returns the URL.

    ``file_path`` must be absolute — it is read from *this* container's filesystem.
    """
    path = Path(file_path)
    if not path.is_absolute():
        raise ValueError(f"file_path must be absolute: {file_path}")
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    pdf_name = path.stem + ".pdf"
    md_text = path.read_text()
    service = _get_service()

    def _render_and_upload() -> str:
        html_body = markdown.markdown(
            md_text, extensions=["fenced_code", "tables", "toc"]
        )
        html = (
            f"<html><head><style>{_PDF_CSS}</style></head>"
            f"<body>{html_body}</body></html>"
        )
        pdf_bytes = HTML(string=html).write_pdf()
        log.info("UploadMarkdownAsPDF | rendered %s (%d bytes)", pdf_name, len(pdf_bytes))

        media = MediaIoBaseUpload(
            io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False
        )

        q_name = _escape_drive_query(pdf_name)
        q_folder = _escape_drive_query(folder_id)
        existing = (
            service.files()
            .list(
                q=f"name='{q_name}' and '{q_folder}' in parents and trashed=false",
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )

        if existing:
            fid = existing[0]["id"]
            service.files().update(
                fileId=fid, media_body=media, supportsAllDrives=True
            ).execute()
            log.info("UploadMarkdownAsPDF | updated existing file id=%s", fid)
        else:
            file_metadata: dict[str, Any] = {"name": pdf_name, "parents": [folder_id]}
            result = (
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            fid = result["id"]
            log.info("UploadMarkdownAsPDF | created new file id=%s", fid)

        return fid

    fid = await asyncio.to_thread(_render_and_upload)
    drive_url = f"https://drive.google.com/file/d/{fid}/view"
    log.info("UploadMarkdownAsPDF | uploaded %s -> %s", pdf_name, drive_url)
    return drive_url


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
