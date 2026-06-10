"""The mcp-playwright catalog (chief.tools.browser.mcp): names, partitions, config."""

from chief.tools.browser import mcp

# ---- catalog drift guard --------------------------------------------------------


def test_catalog_is_subset_of_default_076_tool_set() -> None:
    # Guard against catalog drift: every tool name chief lists must exist in the
    # pinned default tool set exposed by @playwright/mcp@0.0.76 with no --caps.
    # The constant is vendored in mcp.py so this test needs no Docker / network.
    all_catalog = set(mcp.READ_TOOLS) | set(mcp.WRITE_TOOLS)
    # Strip the mcp__playwright__ prefix for comparison against the bare names.
    bare_catalog = {t.removeprefix("mcp__playwright__") for t in all_catalog}
    phantom = bare_catalog - mcp.PLAYWRIGHT_076_DEFAULT_TOOLS
    assert not phantom, (
        f"Tool(s) in chief's catalog are NOT in the @playwright/mcp@0.0.76 "
        f"default-enabled tool set: {sorted(phantom)}. "
        "Remove phantom names or enable the required --caps."
    )


def test_browser_network_request_singular_in_read_tools() -> None:
    # browser_network_request (singular) is a default-enabled read tool that
    # returns full details for a single request; it was missing from READ_TOOLS.
    assert "mcp__playwright__browser_network_request" in mcp.READ_TOOLS


def test_no_vision_tools_without_caps() -> None:
    # Vision tools (browser_mouse_*) require --caps=vision; they must not appear
    # in the catalog unless that cap is added to the CMD.
    vision_tools = {
        t
        for t in set(mcp.READ_TOOLS) | set(mcp.WRITE_TOOLS)
        if "mouse" in t
    }
    assert not vision_tools, (
        f"Vision tools appear in catalog but --caps=vision is not in the CMD: "
        f"{sorted(vision_tools)}"
    )


# ---- tool name tests (server-qualified) ----------------------------------------


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
