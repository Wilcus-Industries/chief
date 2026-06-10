"""The mcp-gmail catalog (chief.tools.gmail.mcp): names, partitions, config."""

from chief.tools.gmail import mcp


def test_tool_names_are_server_qualified() -> None:
    assert mcp.SERVER_NAME == "gmail"
    assert "mcp__gmail__gmail_search_messages" in mcp.READ_TOOLS
    assert "mcp__gmail__gmail_send_message" in mcp.WRITE_TOOLS
    assert "mcp__gmail__gmail_delete_draft" in mcp.DEFERRED_TOOLS


def test_read_write_deferred_are_disjoint() -> None:
    read, write, deferred = (
        set(mcp.READ_TOOLS),
        set(mcp.WRITE_TOOLS),
        set(mcp.DEFERRED_TOOLS),
    )
    assert read & write == set()
    assert read & deferred == set()
    assert write & deferred == set()


def test_send_is_a_write_not_a_read() -> None:
    # Sending mail must ASK (approval card), never ALLOW silently.
    assert "mcp__gmail__gmail_send_message" not in mcp.READ_TOOLS
    assert "mcp__gmail__gmail_send_message" in mcp.WRITE_TOOLS


def test_permanent_deletes_are_deferred() -> None:
    # Permanent, irreversible deletes are hard-blocked; reversible trash is a write.
    assert "mcp__gmail__gmail_delete_draft" in mcp.DEFERRED_TOOLS
    assert "mcp__gmail__gmail_delete_label" in mcp.DEFERRED_TOOLS
    assert "mcp__gmail__gmail_trash_message" in mcp.WRITE_TOOLS
    assert "mcp__gmail__gmail_untrash_message" in mcp.WRITE_TOOLS


def test_chief_send_and_reply_route_through_approval() -> None:
    # On the chief-owned server, send/reply are writes (ASK) — never pre-approved reads.
    svc = mcp.chief_service("http://mcp-gmail-chief:8005/mcp")
    assert "mcp__gmail_chief__gmail_send_message" in svc.write_tools
    assert "mcp__gmail_chief__gmail_reply_on_message" in svc.write_tools
    assert "mcp__gmail_chief__gmail_send_message" not in svc.read_tools
    assert "mcp__gmail_chief__gmail_reply_on_message" not in svc.read_tools


def test_service_bundles_url_and_streamable_http_config() -> None:
    svc = mcp.service("http://mcp-gmail:8004/mcp")

    assert svc.name == "gmail"
    assert svc.server_name == "gmail"
    assert svc.read_tools == mcp.READ_TOOLS
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.deferred_tools == mcp.DEFERRED_TOOLS
    assert svc.server_config() == {
        "type": "http",
        "url": "http://mcp-gmail:8004/mcp",
    }
