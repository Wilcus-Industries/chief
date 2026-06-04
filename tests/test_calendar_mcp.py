"""The mcp-calendar catalog (chief.tools.calendar.mcp): names, partitions, config."""

from chief.tools.calendar import mcp


def test_tool_names_are_server_qualified() -> None:
    assert mcp.SERVER_NAME == "calendar"
    assert "mcp__calendar__list-events" in mcp.READ_TOOLS
    assert "mcp__calendar__create-event" in mcp.WRITE_TOOLS
    assert "mcp__calendar__delete-event" in mcp.DEFERRED_TOOLS


def test_read_write_deferred_are_disjoint() -> None:
    read, write, deferred = (
        set(mcp.READ_TOOLS),
        set(mcp.WRITE_TOOLS),
        set(mcp.DEFERRED_TOOLS),
    )
    assert read & write == set()
    assert read & deferred == set()
    assert write & deferred == set()


def test_freebusy_is_a_read_tool() -> None:
    # Free/busy is the booking flow's availability check — must ALLOW, not ASK.
    assert "mcp__calendar__get-freebusy" in mcp.READ_TOOLS


def test_service_bundles_url_and_streamable_http_config() -> None:
    svc = mcp.service("http://mcp-calendar:8003/mcp")

    assert svc.name == "calendar"
    assert svc.server_name == "calendar"
    assert svc.read_tools == mcp.READ_TOOLS
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.deferred_tools == mcp.DEFERRED_TOOLS
    assert svc.server_config() == {
        "type": "http",
        "url": "http://mcp-calendar:8003/mcp",
    }
