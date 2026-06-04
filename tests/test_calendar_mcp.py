"""The mcp-gcal catalog (``chief.tools.calendar.mcp``): names, partitions, config."""

from chief.tools.calendar import mcp


def test_tool_names_are_server_qualified() -> None:
    assert mcp.SERVER_NAME == "gcal"
    assert "mcp__gcal__list-events" in mcp.READ_TOOLS
    assert "mcp__gcal__create-event" in mcp.WRITE_TOOLS
    assert "mcp__gcal__delete-event" in mcp.DEFERRED_TOOLS


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
    assert "mcp__gcal__get-freebusy" in mcp.READ_TOOLS


def test_server_config_is_streamable_http() -> None:
    config = mcp.server_config("http://mcp-gcal:3000/")

    assert config == {"type": "http", "url": "http://mcp-gcal:3000/"}
