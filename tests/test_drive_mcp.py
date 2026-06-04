"""The mcp-drive catalog (``chief.tools.drive.mcp``): names, partitions, config."""

from chief.tools.drive import mcp


def test_read_and_write_tools_are_qualified() -> None:
    assert mcp.SERVER_NAME == "drive"
    assert mcp.READ_TOOLS == ("mcp__drive__ReadDriveFile",)
    assert mcp.WRITE_TOOLS == ("mcp__drive__UploadMarkdownAsPDF",)
    assert mcp.DEFERRED_TOOLS == ()


def test_read_and_write_are_disjoint() -> None:
    assert set(mcp.READ_TOOLS) & set(mcp.WRITE_TOOLS) == set()


def test_service_bundles_url_and_streamable_http_config() -> None:
    svc = mcp.service("http://mcp-drive:8001/mcp")

    assert svc.name == "drive"
    assert svc.read_tools == mcp.READ_TOOLS
    assert svc.write_tools == mcp.WRITE_TOOLS
    assert svc.server_config() == {"type": "http", "url": "http://mcp-drive:8001/mcp"}
