"""The mcp-playwright catalog (chief.tools.browser.mcp): names, partitions, config."""

from chief.tools.browser import mcp


def test_tool_names_are_server_qualified() -> None:
    # SDK qualifies MCP tool names as mcp__<server>__<tool>.
    assert mcp.SERVER_NAME == "playwright"
    assert all(t.startswith("mcp__playwright__") for t in mcp.READ_TOOLS)
    assert all(t.startswith("mcp__playwright__") for t in mcp.WRITE_TOOLS)


def test_read_write_partitions_are_disjoint() -> None:
    read, write = set(mcp.READ_TOOLS), set(mcp.WRITE_TOOLS)
    assert read & write == set()


def test_read_partition_covers_navigate_snapshot_screenshot_and_inspection() -> None:
    assert "mcp__playwright__browser_navigate" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_snapshot" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_take_screenshot" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_console_messages" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_network_requests" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_wait_for" in mcp.READ_TOOLS
    assert "mcp__playwright__browser_tabs" in mcp.READ_TOOLS


def test_write_partition_covers_click_type_fill_select_drag_upload_dialogs() -> None:
    assert "mcp__playwright__browser_click" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_type" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_fill_form" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_select_option" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_drag" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_file_upload" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_handle_dialog" in mcp.WRITE_TOOLS


def test_evaluate_and_run_code_are_in_writes_not_reads() -> None:
    # Arbitrary-JS tools must sit in the write partition per the issue.
    assert "mcp__playwright__browser_evaluate" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_run_code_unsafe" in mcp.WRITE_TOOLS
    assert "mcp__playwright__browser_evaluate" not in mcp.READ_TOOLS
    assert "mcp__playwright__browser_run_code_unsafe" not in mcp.READ_TOOLS


def test_no_deferred_tools() -> None:
    # Browser has no permanently-blocked (deferred) ops.
    assert mcp.DEFERRED_TOOLS == ()


def test_service_factory_returns_bundle_with_correct_shape() -> None:
    url = "http://mcp-playwright:3000/mcp"
    svc = mcp.service(url)

    assert svc.name == "browser"
    assert svc.server_name == "playwright"
    assert svc.read_tools == mcp.READ_TOOLS
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.deferred_tools == ()
    assert svc.server_config() == {"type": "http", "url": url}
