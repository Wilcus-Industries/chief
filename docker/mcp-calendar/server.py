"""chief's own Google Calendar MCP server — FastMCP over streamable-HTTP.

Replaces the abandoned nspady google-calendar-mcp. A thin FastMCP server on top of
google-api-python-client. Crucially: each chief TaskSession opens its *own* MCP
connection, and FastMCP's session manager gives every connection a fresh transport — so
the nspady "Server already initialized" single-shared-transport failure (which killed
calendar after the first task) cannot recur here.

Auth — multi-account (issue #46, fixed in issue #56):
    The server scans TOKEN_DIR (default ``/token``) for all ``google_token*.json``
    files and loads them into a per-label credential registry at startup. On each
    ``tools/call``, ``_get_service()`` reads ``X-Account-Label`` directly from the
    Starlette request object exposed via FastMCP's per-call ``request_context``
    (``mcp.get_context().request_context.request``).  This is genuinely
    per-request-scoped: the MCP lowlevel server sets the ``request_ctx`` ContextVar
    *inside* the spawned task that handles each message, so it is never frozen at
    session-start.  Falls back to the first (default) credential when no header is
    present — fully backward-compatible with single-account deploys.

    The earlier approach used a Starlette ASGI middleware writing to a module-level
    ContextVar.  That broke because FastMCP (stateful mode) dispatches tool handlers
    in a *persistent session task* that was spawned before the current HTTP request
    arrived; anyio memory-stream boundaries do not propagate contextvars, so the
    handler always saw the value frozen at session creation.

    Credentials are refreshed in memory only — no write-back. Three containers share
    one token file; letting google-api-python-client refresh the access token in memory
    on each call (and never persisting) keeps a single writer (the sheets container) and
    dodges a write race. The refresh_token — the part that must persist — is unchanged
    by a refresh.

Tool names are hyphenated (``@mcp.tool(name=...)``) to match chief's
``src/chief/tools/calendar/mcp.py`` catalog. This file is standalone: it imports nothing
from ``chief`` and ships in its own image with its own requirements.

Reuse pattern for #47 / #48 (Drive, Gmail):
    Copy ``_get_service()`` verbatim, replacing ``_service_registry`` /
    ``_default_service`` with the equivalent registry for your service.  No
    middleware, no ContextVar.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP
from owner_tz import owner_tz_from_config, resolve_owner_tz
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("calendar.server")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
#: Legacy single-token path (backward compat). When present it is always the default.
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/token/google_token.json")
#: Directory scanned for all ``google_token*.json`` files.
TOKEN_DIR = os.environ.get("TOKEN_DIR", str(Path(TOKEN_PATH).parent))
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
# config.yaml (mounted read-only) is the source of truth; OWNER_TZ env overrides it.
_configured_tz = (os.environ.get("OWNER_TZ") or "").strip() or owner_tz_from_config(
    CONFIG_PATH
)
OWNER_TZ = resolve_owner_tz(os.environ.get("OWNER_TZ"), CONFIG_PATH)
if _configured_tz and _configured_tz != OWNER_TZ:
    # A typo shouldn't silently run in UTC — the very bug this resolution fixes.
    log.warning("owner_tz %r is not a known IANA zone; using %s", _configured_tz, OWNER_TZ)
PORT = int(os.environ.get("PORT", "8003"))

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


# ---------------------------------------------------------------------------
# Per-request credential/service selection (dynamic re-scan, issue #50)
# ---------------------------------------------------------------------------

# No boot-time registry build: we re-scan TOKEN_DIR on every request so a token
# dropped after the server starts is picked up immediately without a restart.
# For a handful of token files the directory scan is negligible.

_token_dir = Path(TOKEN_DIR)

log.info("Calendar MCP ready | TOKEN_DIR=%s tz=%s", TOKEN_DIR, OWNER_TZ)


def _build_service_registry() -> tuple[dict[str, Any], Any]:
    """Scan TOKEN_DIR and build a label → service dict.

    Returns ``(registry, default_service)`` where ``default_service`` is the first
    entry (the legacy/primary account) or ``None`` when the dir is empty.

    Falls back to the legacy single-file TOKEN_PATH when the directory scan yields
    nothing — preserves backward compat for single-token deploys.
    """
    creds_registry = _load_credentials(_token_dir)

    if not creds_registry:
        # Legacy fallback: try the single TOKEN_PATH.
        try:
            legacy_creds = Credentials.from_authorized_user_file(
                TOKEN_PATH, scopes=SCOPES
            )
            if legacy_creds.expired and legacy_creds.refresh_token:
                legacy_creds.refresh(Request())
            creds_registry["_legacy"] = legacy_creds
        except Exception:  # noqa: BLE001
            pass  # No credentials at all; tools will error on the actual call.

    registry: dict[str, Any] = {}
    for lbl, creds in creds_registry.items():
        try:
            registry[lbl] = build(
                "calendar", "v3", credentials=creds, cache_discovery=False
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to build calendar service for %r", lbl, exc_info=True)

    default = next(iter(registry.values()), None)
    return registry, default


def _get_service_for_label(label: str | None) -> Any:
    """Return the calendar service for ``label``, re-scanning TOKEN_DIR each call.

    A token dropped after module load is discovered here because we call
    ``_build_service_registry()`` on every invocation.  Falls back to the default
    (first) service when ``label`` is absent or not in the registry.
    """
    registry, default = _build_service_registry()
    if label and label in registry:
        return registry[label]
    return default


def _get_service() -> Any:
    """Return the calendar service resource for the current request.

    Reads ``X-Account-Label`` from the per-call Starlette request exposed by
    FastMCP's ``request_context`` (set by the MCP lowlevel server *per handler
    invocation*, not per session).  Falls back to the default (first loaded)
    service when the label is absent or unknown — backward compat for
    single-account deploys and requests that carry no header.

    Re-scans TOKEN_DIR on every call (issue #50) so tokens dropped at runtime
    are picked up without a restart.

    Why not a ContextVar?  FastMCP (stateful mode) dispatches tool handlers in
    a persistent session task spawned at ``initialize`` time; a middleware-set
    ContextVar set on a later HTTP POST is invisible to that task because anyio
    memory-stream boundaries do not propagate contextvars.  Reading from
    ``request_context.request`` is safe because the MCP lowlevel server calls
    ``request_ctx.set()`` *inside* each per-message spawned task before
    invoking the handler — so it is always scoped to the individual call.
    """
    label: str | None = None
    try:
        req = mcp.get_context().request_context.request
        if req is not None:
            label = req.headers.get(_ACCOUNT_HEADER)
    except LookupError:
        # Called outside a request context (e.g. at import time or in tests
        # that don't go through the ASGI path).  Fall back to default.
        pass
    return _get_service_for_label(label)


# ---------------------------------------------------------------------------
# FastMCP server and tools
# ---------------------------------------------------------------------------

mcp = FastMCP("calendar", host="0.0.0.0", port=PORT)

_HAS_OFFSET = re.compile(r"(Z|[+-]\d\d:\d\d)$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _json(obj: Any) -> str:
    """Serialize a Google API response (or our own dict) to a compact JSON string."""
    return json.dumps(obj, default=str, ensure_ascii=False)


def _ensure_offset(ts: str) -> str:
    """Google events.list/freebusy require an offset on timeMin/timeMax; assume UTC."""
    return ts if _HAS_OFFSET.search(ts) else ts + "Z"


def _time_field(value: str, tz: str) -> dict[str, str]:
    """An event start/end: ``{date}`` for an all-day value, else ``{dateTime, timeZone}``.

    A value that already carries its own offset is passed through without ``timeZone``.
    """
    if _DATE_ONLY.match(value):
        return {"date": value}
    field = {"dateTime": value}
    if not _HAS_OFFSET.search(value):
        field["timeZone"] = tz
    return field


def _event_summary(e: dict[str, Any]) -> dict[str, Any]:
    """The handful of event fields the model actually reasons over (keeps payloads small)."""
    return {
        "id": e.get("id"),
        "summary": e.get("summary", ""),
        "start": e.get("start", {}),
        "end": e.get("end", {}),
        "status": e.get("status", ""),
        "location": e.get("location", ""),
        "htmlLink": e.get("htmlLink", ""),
    }


@mcp.tool(name="list-calendars")
async def list_calendars() -> str:
    """List the calendars on the account (id, summary, access role)."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        items = service.calendarList().list().execute().get("items", [])
        return [
            {
                "id": c["id"],
                "summary": c.get("summary", ""),
                "accessRole": c.get("accessRole", ""),
                "primary": c.get("primary", False),
            }
            for c in items
        ]

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="list-events")
async def list_events(
    calendarId: str,
    timeMin: str,
    timeMax: str,
    query: str | None = None,
    maxResults: int = 50,
) -> str:
    """List events in [timeMin, timeMax) (RFC3339). Optional free-text ``query``."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        resp = (
            service.events()
            .list(
                calendarId=calendarId,
                timeMin=_ensure_offset(timeMin),
                timeMax=_ensure_offset(timeMax),
                singleEvents=True,
                orderBy="startTime",
                q=query,
                maxResults=maxResults,
                timeZone=OWNER_TZ,
            )
            .execute()
        )
        return [_event_summary(e) for e in resp.get("items", [])]

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="get-event")
async def get_event(calendarId: str, eventId: str) -> str:
    """Fetch a single event by id (the full Google event resource)."""
    service = _get_service()

    def _call() -> dict[str, Any]:
        return service.events().get(calendarId=calendarId, eventId=eventId).execute()

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="get-freebusy")
async def get_freebusy(calendarId: str, timeMin: str, timeMax: str) -> str:
    """Busy intervals on ``calendarId`` within [timeMin, timeMax) (RFC3339)."""
    service = _get_service()

    def _call() -> dict[str, Any]:
        body = {
            "timeMin": _ensure_offset(timeMin),
            "timeMax": _ensure_offset(timeMax),
            "timeZone": OWNER_TZ,
            "items": [{"id": calendarId}],
        }
        resp = service.freebusy().query(body=body).execute()
        return resp.get("calendars", {}).get(calendarId, {})

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="get-current-time")
async def get_current_time() -> str:
    """Current date/time in the owner's timezone — anchor for relative booking."""
    now = dt.datetime.now(ZoneInfo(OWNER_TZ))
    return _json(
        {
            "timezone": OWNER_TZ,
            "now": now.isoformat(),
            "weekday": now.strftime("%A"),
        }
    )


@mcp.tool(name="create-event")
async def create_event(
    calendarId: str,
    summary: str,
    start: str,
    end: str,
    timeZone: str | None = None,
    eventId: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
) -> str:
    """Create an event. ``start``/``end`` are ``YYYY-MM-DD`` (all-day) or a dateTime."""
    tz = timeZone or OWNER_TZ
    body: dict[str, Any] = {
        "summary": summary,
        "start": _time_field(start, tz),
        "end": _time_field(end, tz),
    }
    if eventId:
        body["id"] = eventId
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]

    service = _get_service()

    def _call() -> dict[str, Any]:
        return service.events().insert(calendarId=calendarId, body=body).execute()

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="update-event")
async def update_event(
    calendarId: str,
    eventId: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    timeZone: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> str:
    """Patch an existing event — only the supplied fields change."""
    tz = timeZone or OWNER_TZ
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        body["start"] = _time_field(start, tz)
    if end is not None:
        body["end"] = _time_field(end, tz)
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location

    service = _get_service()

    def _call() -> dict[str, Any]:
        return (
            service.events()
            .patch(calendarId=calendarId, eventId=eventId, body=body)
            .execute()
        )

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="delete-event")
async def delete_event(calendarId: str, eventId: str) -> str:
    """Delete an event. chief hard-blocks this (DEFERRED_TOOLS); the live test uses it."""
    service = _get_service()

    def _call() -> dict[str, Any]:
        service.events().delete(calendarId=calendarId, eventId=eventId).execute()
        return {"deleted": eventId}

    return _json(await asyncio.to_thread(_call))


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: StarletteRequest) -> JSONResponse:
    """Liveness probe for the compose healthcheck."""
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import anyio
    import uvicorn

    async def _serve() -> None:
        # No middleware wrapper needed: X-Account-Label is read inside _get_service()
        # directly from FastMCP's per-call request_context — no ContextVar required.
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
