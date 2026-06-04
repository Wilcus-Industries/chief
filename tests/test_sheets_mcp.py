"""The mcp-sheets catalog (``chief.tools.sheets.mcp``): names, partitions, config."""

from chief.tools.sheets import mcp


def test_reads_are_qualified_and_listed() -> None:
    assert mcp.SERVER_NAME == "sheets"
    assert "mcp__sheets__get_sheet_data" in mcp.READ_TOOLS
    assert "mcp__sheets__list_spreadsheets" in mcp.READ_TOOLS


def test_writes_include_cell_and_share_ops() -> None:
    # share_spreadsheet grants others access, so it is approval-gated like a write.
    assert "mcp__sheets__update_cells" in mcp.WRITE_TOOLS
    assert "mcp__sheets__share_spreadsheet" in mcp.WRITE_TOOLS


def test_read_and_write_are_disjoint() -> None:
    assert set(mcp.READ_TOOLS) & set(mcp.WRITE_TOOLS) == set()
    assert mcp.DEFERRED_TOOLS == ()


def test_service_bundles_url_and_streamable_http_config() -> None:
    svc = mcp.service("http://mcp-sheets:8002/mcp")

    assert svc.name == "sheets"
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.server_config() == {"type": "http", "url": "http://mcp-sheets:8002/mcp"}
