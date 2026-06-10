"""The chief-owned gmail catalog (chief.tools.gmail.mcp): names, partitions, config.

After the issue #52 cutover there is a single Gmail server — chief's own — wired via
``chief_service``.  All tool names are SDK-qualified under the ``gmail_chief`` server.
"""

from chief.tools.gmail import mcp


def test_tool_names_are_server_qualified() -> None:
    assert mcp.CHIEF_SERVER_NAME == "gmail_chief"
    assert "mcp__gmail_chief__gmail_search_messages" in mcp.READ_TOOLS
    assert "mcp__gmail_chief__gmail_send_message" in mcp.WRITE_TOOLS
    assert "mcp__gmail_chief__gmail_delete_draft" in mcp.DEFERRED_TOOLS


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
    assert "mcp__gmail_chief__gmail_send_message" not in mcp.READ_TOOLS
    assert "mcp__gmail_chief__gmail_send_message" in mcp.WRITE_TOOLS


def test_remaining_writes_are_writes() -> None:
    # Drafts, label, and trash mutations all route through the approval card.
    for tool in (
        "gmail_create_draft",
        "gmail_update_draft",
        "gmail_send_draft",
        "gmail_create_label",
        "gmail_modify_message_labels",
        "gmail_trash_message",
        "gmail_untrash_message",
    ):
        qualified = f"mcp__gmail_chief__{tool}"
        assert qualified in mcp.WRITE_TOOLS, f"{tool} must be a write tool"
        assert qualified not in mcp.READ_TOOLS


def test_permanent_deletes_are_deferred() -> None:
    # Permanent, irreversible deletes are hard-blocked; reversible trash is a write.
    assert "mcp__gmail_chief__gmail_delete_draft" in mcp.DEFERRED_TOOLS
    assert "mcp__gmail_chief__gmail_delete_label" in mcp.DEFERRED_TOOLS
    assert "mcp__gmail_chief__gmail_trash_message" in mcp.WRITE_TOOLS
    assert "mcp__gmail_chief__gmail_untrash_message" in mcp.WRITE_TOOLS


def test_chief_send_and_reply_route_through_approval() -> None:
    # On the chief-owned server, send/reply are writes (ASK) — never pre-approved reads.
    svc = mcp.chief_service("http://mcp-gmail:8004/mcp")
    assert "mcp__gmail_chief__gmail_send_message" in svc.write_tools
    assert "mcp__gmail_chief__gmail_reply_on_message" in svc.write_tools
    assert "mcp__gmail_chief__gmail_send_message" not in svc.read_tools
    assert "mcp__gmail_chief__gmail_reply_on_message" not in svc.read_tools


def test_chief_service_bundles_url_and_streamable_http_config() -> None:
    svc = mcp.chief_service("http://mcp-gmail:8004/mcp")

    assert svc.name == "gmail_chief"
    assert svc.server_name == "gmail_chief"
    assert svc.read_tools == mcp.READ_TOOLS
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.deferred_tools == mcp.DEFERRED_TOOLS
    assert svc.server_config() == {
        "type": "http",
        "url": "http://mcp-gmail:8004/mcp",
    }


def test_chief_service_forwards_account_header() -> None:
    # The X-Account-Label header threads through for per-request account selection.
    svc = mcp.chief_service(
        "http://mcp-gmail:8004/mcp", headers={"X-Account-Label": "work@corp.com"}
    )
    config = svc.server_config()
    assert config["headers"] == {"X-Account-Label": "work@corp.com"}
