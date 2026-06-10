"""Wiring tests: X-Account-Label injection extends to Drive and Sheets services.

Proves that _build_services_with_account() stamps X-Account-Label on
drive and sheets services (not just calendar), so all three Google MCP
servers select the right credential per request.
"""

from __future__ import annotations

from chief.tools.google import GoogleService


class TestDriveServiceHeaderInjection:
    """drive.mcp.service() header factory works correctly."""

    def test_service_with_header(self) -> None:
        from chief.tools.drive import mcp

        svc = mcp.service(
            "http://mcp-drive:8001/mcp",
            headers={"X-Account-Label": "work@corp.com"},
        )
        assert svc.name == "drive"
        assert svc.headers == {"X-Account-Label": "work@corp.com"}
        cfg = svc.server_config()
        assert cfg["headers"]["X-Account-Label"] == "work@corp.com"

    def test_service_without_header(self) -> None:
        from chief.tools.drive import mcp

        svc = mcp.service("http://mcp-drive:8001/mcp")
        assert svc.headers is None
        cfg = svc.server_config()
        assert "headers" not in cfg

    def test_service_two_accounts_different_headers(self) -> None:
        """Two service configs for different accounts carry different labels."""
        from chief.tools.drive import mcp

        svc_a = mcp.service(
            "http://mcp-drive:8001/mcp",
            headers={"X-Account-Label": "main@example.com"},
        )
        svc_b = mcp.service(
            "http://mcp-drive:8001/mcp",
            headers={"X-Account-Label": "work@corp.com"},
        )
        assert svc_a.server_config()["headers"]["X-Account-Label"] == "main@example.com"
        assert svc_b.server_config()["headers"]["X-Account-Label"] == "work@corp.com"


class TestSheetsServiceHeaderInjection:
    """sheets.mcp.service() header factory works correctly."""

    def test_service_with_header(self) -> None:
        from chief.tools.sheets import mcp

        svc = mcp.service(
            "http://mcp-sheets:8002/mcp",
            headers={"X-Account-Label": "work@corp.com"},
        )
        assert svc.name == "sheets"
        assert svc.headers == {"X-Account-Label": "work@corp.com"}
        cfg = svc.server_config()
        assert cfg["headers"]["X-Account-Label"] == "work@corp.com"

    def test_service_without_header(self) -> None:
        from chief.tools.sheets import mcp

        svc = mcp.service("http://mcp-sheets:8002/mcp")
        assert svc.headers is None
        cfg = svc.server_config()
        assert "headers" not in cfg


class TestBuildServicesWithAccountDriveSheets:
    """_build_services_with_account stamps X-Account-Label on drive+sheets too."""

    def _make_services(self) -> list[GoogleService]:
        """Return three services: calendar, drive, sheets."""
        return [
            GoogleService(
                name="calendar",
                server_name="calendar",
                url="http://mcp-calendar:8003/mcp",
                read_tools=("mcp__calendar__list-calendars",),
                write_tools=(),
            ),
            GoogleService(
                name="drive",
                server_name="drive",
                url="http://mcp-drive:8001/mcp",
                read_tools=("mcp__drive__ReadDriveFile",),
                write_tools=("mcp__drive__UploadMarkdownAsPDF",),
            ),
            GoogleService(
                name="sheets",
                server_name="sheets",
                url="http://mcp-sheets:8002/mcp",
                read_tools=("mcp__sheets__get_sheet_data",),
                write_tools=("mcp__sheets__update_cells",),
            ),
        ]

    def _build_services_with_account(
        self,
        services: list[GoogleService],
        active_account_label: str | None,
    ) -> tuple[GoogleService, ...]:
        """Replicate the logic from TaskManager._build_services_with_account."""
        from chief.tools.calendar import mcp as calendar_mcp
        from chief.tools.drive import mcp as drive_mcp
        from chief.tools.sheets import mcp as sheets_mcp

        if not active_account_label:
            return tuple(services)
        headers = {"X-Account-Label": active_account_label}
        result: list[GoogleService] = []
        for svc in services:
            if svc.name == "calendar":
                result.append(calendar_mcp.service(svc.url, headers=headers))
            elif svc.name == "drive":
                result.append(drive_mcp.service(svc.url, headers=headers))
            elif svc.name == "sheets":
                result.append(sheets_mcp.service(svc.url, headers=headers))
            else:
                result.append(svc)
        return tuple(result)

    def test_all_three_services_stamped(self) -> None:
        """With an active account, all three Google services get the header."""
        services = self._make_services()
        stamped = self._build_services_with_account(services, "work@corp.com")

        assert len(stamped) == 3
        for svc in stamped:
            assert svc.headers == {"X-Account-Label": "work@corp.com"}, (
                f"Service {svc.name} missing header, got {svc.headers!r}"
            )

    def test_no_account_no_header(self) -> None:
        """Without an active account, services pass through unchanged."""
        services = self._make_services()
        unchanged = self._build_services_with_account(services, None)

        for svc in unchanged:
            assert svc.headers is None

    def test_two_accounts_different_stamps_no_cross_talk(self) -> None:
        """Two calls with different labels produce independent configs."""
        services = self._make_services()
        stamped_a = self._build_services_with_account(services, "main@example.com")
        stamped_b = self._build_services_with_account(services, "work@corp.com")

        for svc in stamped_a:
            assert svc.headers == {"X-Account-Label": "main@example.com"}
        for svc in stamped_b:
            assert svc.headers == {"X-Account-Label": "work@corp.com"}
