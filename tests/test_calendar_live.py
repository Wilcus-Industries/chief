"""Opt-in live test against a real Google Calendar through a running mcp-gcal.

Skipped unless ``CHIEF_GCAL_LIVE`` is set — this is the manual "does the wiring actually
reach Google" check from the M5 plan, not a CI test. It talks to the MCP server directly
over streamable HTTP (no Claude model, deterministic, no token spend), exercising the
read + write tools end to end: list-events → create-event → update-event → get-freebusy,
then deletes its own event so the calendar is left clean.

Run it:

1. Mint the token:  ``python -m chief.tools.calendar.auth``
2. Start mcp-gcal with a temporary published port (the compose service deliberately has
   none):  ``docker compose --profile calendar run --rm --service-ports mcp-gcal``
3. Set ``CHIEF_GCAL_LIVE=1`` and ``CHIEF_GCAL_TEST_CALENDAR=<throwaway-calendar-id>``,
   then run ``uv run pytest tests/test_calendar_live.py``.

``CHIEF_GCAL_TEST_CALENDAR`` must be a *throwaway* calendar id — the test creates and
mutates an event there. ``CHIEF_GCAL_MCP_URL`` (default ``http://localhost:3000/``) and
``CHIEF_GCAL_TEST_TZ`` (default ``UTC``) override the endpoint and event timezone.
"""

import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from chief.tools.calendar import mcp as calendar_mcp

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHIEF_GCAL_LIVE"),
    reason="live calendar test — set CHIEF_GCAL_LIVE=1 (needs a running mcp-gcal)",
)


def _bare(qualified: str) -> str:
    """Strip the SDK ``mcp__<server>__`` prefix → the MCP server's own tool name."""
    return qualified.removeprefix(f"mcp__{calendar_mcp.SERVER_NAME}__")


def _text(result: object) -> str:
    """Flatten an MCP tool result's content blocks into one string for assertions."""
    content = getattr(result, "content", []) or []
    return "".join(getattr(block, "text", "") for block in content)


@pytest.mark.timeout(120)  # live network round-trips; override the 30s global cap
async def test_live_list_create_update_freebusy() -> None:
    calendar_id = os.environ.get("CHIEF_GCAL_TEST_CALENDAR")
    if not calendar_id:
        pytest.skip("set CHIEF_GCAL_TEST_CALENDAR to a throwaway calendar id")
    url = os.environ.get("CHIEF_GCAL_MCP_URL", "http://localhost:3000/")
    tz = os.environ.get("CHIEF_GCAL_TEST_TZ", "UTC")

    # Our own event id (base32hex: hex digits are a valid subset) so update + delete
    # need no id-parsing of the create response.
    event_id = "chieflive" + secrets.token_hex(10)
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(hours=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    window = {
        "timeMin": (start - timedelta(hours=1)).strftime(fmt),
        "timeMax": (end + timedelta(hours=1)).strftime(fmt),
    }

    # Tool names come from the wiring catalog, so a rename there fails this test.
    assert "mcp__gcal__create-event" in calendar_mcp.WRITE_TOOLS
    assert "mcp__gcal__delete-event" in calendar_mcp.DEFERRED_TOOLS

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            created = False
            try:
                listed = await session.call_tool(
                    "list-events", {"calendarId": calendar_id, **window}
                )
                assert not listed.isError, _text(listed)

                created_res = await session.call_tool(
                    "create-event",
                    {
                        "calendarId": calendar_id,
                        "eventId": event_id,
                        "summary": "chief live test",
                        "start": start.strftime(fmt),
                        "end": end.strftime(fmt),
                        "timeZone": tz,
                    },
                )
                assert not created_res.isError, _text(created_res)
                created = True

                updated = await session.call_tool(
                    "update-event",
                    {
                        "calendarId": calendar_id,
                        "eventId": event_id,
                        "summary": "chief live test (updated)",
                        "timeZone": tz,
                    },
                )
                assert not updated.isError, _text(updated)

                freebusy = await session.call_tool(
                    "get-freebusy",
                    {"calendars": [{"id": calendar_id}], **window},
                )
                assert not freebusy.isError, _text(freebusy)
            finally:
                if created:
                    try:
                        await session.call_tool(
                            "delete-event",
                            {"calendarId": calendar_id, "eventId": event_id},
                        )
                    except Exception:  # cleanup is best-effort
                        pass
