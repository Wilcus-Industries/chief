"""Google Drive MCP server — FastMCP over streamable-HTTP (ported from genesis-x).

Two tools: ``ReadDriveFile`` (read a Doc / PDF / Office file by Drive URL) and
``UploadMarkdownAsPDF`` (render a local Markdown file to PDF and upload it to a folder).
Ported from genesis-x's ``drive/server.py`` with three changes for chief:

- transport ``sse`` → ``streamable-http`` (chief reaches it at ``/mcp``),
- a ``/health`` route for the compose healthcheck,
- the token is *not* written back after a refresh. Three containers share one token
  file; the sheets container is the sole writer, so calendar + drive refresh the access
  token in memory only (the refresh_token, the part that persists, is unchanged).

Standalone image — imports nothing from the chief package.
"""

import asyncio
import io
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import markdown
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from drive_query import _escape_drive_query
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
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/token/google_token.json")
PORT = int(os.environ.get("PORT", "8001"))

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


_creds = Credentials.from_authorized_user_file(TOKEN_PATH, scopes=SCOPES)
if _creds.expired and _creds.refresh_token:
    _creds.refresh(Request())  # in memory only — no write-back (single-writer policy)
_service = build("drive", "v3", credentials=_creds, cache_discovery=False)
log.info("Drive MCP authenticated | token=%s", TOKEN_PATH)

mcp = FastMCP("drive", host="0.0.0.0", port=PORT)


@mcp.tool()
async def ReadDriveFile(url: str) -> str:
    """Read the text of a Google Doc, PDF, or Office file (docx/xlsx/pptx) by Drive URL."""
    file_id = _extract_file_id(url)

    def _fetch() -> str:
        meta = _service.files().get(fileId=file_id, fields="mimeType,name").execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)
        log.info("ReadDriveFile | id=%s name=%s mime=%s", file_id, name, mime)
        if mime == "application/vnd.google-apps.document":
            data = (
                _service.files()
                .export(fileId=file_id, mimeType="text/plain")
                .execute()
            )
            return data.decode("utf-8") if isinstance(data, bytes) else data
        if mime.startswith("text/") and mime not in _MIME_EXT:
            data = _service.files().get_media(fileId=file_id).execute()
            return data.decode("utf-8", errors="replace")
        ext = _MIME_EXT.get(mime)
        if not ext:
            raise ValueError(f"Unsupported mime type: {mime} (file: {name})")
        data = _service.files().get_media(fileId=file_id).execute()
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
            _service.files()
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
            _service.files().update(
                fileId=fid, media_body=media, supportsAllDrives=True
            ).execute()
            log.info("UploadMarkdownAsPDF | updated existing file id=%s", fid)
        else:
            file_metadata: dict[str, Any] = {"name": pdf_name, "parents": [folder_id]}
            result = (
                _service.files()
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
    mcp.run(transport="streamable-http")
