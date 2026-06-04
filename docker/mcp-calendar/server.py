"""chief's own Google Calendar MCP server — FastMCP over streamable-HTTP.

Replaces the abandoned nspady google-calendar-mcp. A thin FastMCP server on top of
google-api-python-client. Crucially: each chief TaskSession opens its *own* MCP
connection, and FastMCP's session manager gives every connection a fresh transport — so
the nspady "Server already initialized" single-shared-transport failure (which killed
calendar after the first task) cannot recur here.

Auth: the shared google-auth token (calendar scope) is loaded at module start from
``GOOGLE_TOKEN_PATH`` and refreshed in memory — no write-back. Three containers share one
token file; letting google-api-python-client refresh the access token in memory on each
call (and never persisting) keeps a single writer (the sheets container) and dodges a
write race. The refresh_token — the part that must persist — is unchanged by a refresh.

Tool names are hyphenated (``@mcp.tool(name=...)``) to match chief's
``src/chief/tools/calendar/mcp.py`` catalog. This file is standalone: it imports nothing
from ``chief`` and ships in its own image with its own requirements.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
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
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/token/google_token.json")
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

_creds = Credentials.from_authorized_user_file(TOKEN_PATH, scopes=SCOPES)
if _creds.expired and _creds.refresh_token:
    _creds.refresh(Request())  # in memory only — no write-back (single-writer policy)
# google-api-python-client refreshes the access token in memory on subsequent calls.
_service = build("calendar", "v3", credentials=_creds, cache_discovery=False)
log.info("Calendar MCP authenticated | token=%s tz=%s", TOKEN_PATH, OWNER_TZ)

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

    def _call() -> list[dict[str, Any]]:
        items = _service.calendarList().list().execute().get("items", [])
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

    def _call() -> list[dict[str, Any]]:
        resp = (
            _service.events()
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

    def _call() -> dict[str, Any]:
        return _service.events().get(calendarId=calendarId, eventId=eventId).execute()

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="get-freebusy")
async def get_freebusy(calendarId: str, timeMin: str, timeMax: str) -> str:
    """Busy intervals on ``calendarId`` within [timeMin, timeMax) (RFC3339)."""

    def _call() -> dict[str, Any]:
        body = {
            "timeMin": _ensure_offset(timeMin),
            "timeMax": _ensure_offset(timeMax),
            "timeZone": OWNER_TZ,
            "items": [{"id": calendarId}],
        }
        resp = _service.freebusy().query(body=body).execute()
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

    def _call() -> dict[str, Any]:
        return _service.events().insert(calendarId=calendarId, body=body).execute()

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

    def _call() -> dict[str, Any]:
        return (
            _service.events()
            .patch(calendarId=calendarId, eventId=eventId, body=body)
            .execute()
        )

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="delete-event")
async def delete_event(calendarId: str, eventId: str) -> str:
    """Delete an event. chief hard-blocks this (DEFERRED_TOOLS); the live test uses it."""

    def _call() -> dict[str, Any]:
        _service.events().delete(calendarId=calendarId, eventId=eventId).execute()
        return {"deleted": eventId}

    return _json(await asyncio.to_thread(_call))


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: StarletteRequest) -> JSONResponse:
    """Liveness probe for the compose healthcheck."""
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
