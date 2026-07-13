"""The health page's pluggable checklist (#153).

A health check is just an async callable returning one :class:`HealthItem`, and the
page renders whatever list it is given — so future subsystems (the Mac permissions
doctor, per-sidecar liveness probes) bolt on by appending a callable, no page
changes. :func:`build_health_checks` assembles the v1 set off the boot settings and
live filesystem state (the client-plane socket file); it deliberately makes no
network calls — the page must render instantly on a phone, and "configured" vs
"reachable" is an honest distinction the detail text spells out.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings


@dataclass(frozen=True)
class HealthItem:
    """One line on the health page.

    ``ok=None`` renders as informational (a dot, not a verdict) — for facts that
    are neither good nor bad, like a deliberately disabled option.
    """

    name: str
    ok: bool | None
    detail: str


HealthCheck = Callable[[], Awaitable[HealthItem]]


def _static(name: str, ok: bool | None, detail: str) -> HealthCheck:
    async def check() -> HealthItem:
        return HealthItem(name=name, ok=ok, detail=detail)

    return check


def _socket_check(socket_path: str) -> HealthCheck:
    async def check() -> HealthItem:
        alive = Path(socket_path).exists()
        return HealthItem(
            name="client-plane socket",
            ok=alive,
            detail=socket_path if alive else f"{socket_path} is missing",
        )

    return check


def _platform_item(name: str, configured: bool) -> HealthCheck:
    detail = (
        "connected (token + owner id set)"
        if configured
        else "not configured — connect it in Settings"
    )
    return _static(name, configured if configured else None, detail)


def build_health_checks(settings: Settings) -> list[HealthCheck]:
    """The v1 checklist: adapters, scheduler, sidecars, keys, live socket state."""
    sidecars = (
        ("calendar", settings.calendar_enabled, settings.calendar_mcp_url),
        ("drive", settings.drive_enabled, settings.drive_mcp_url),
        ("sheets", settings.sheets_enabled, settings.sheets_mcp_url),
        ("gmail", settings.gmail_enabled, settings.gmail_mcp_url),
        ("playwright", settings.playwright_enabled, settings.playwright_mcp_url),
    )
    checks: list[HealthCheck] = [
        _socket_check(settings.socket_path),
        _platform_item("telegram adapter", settings.telegram_configured),
        _platform_item("discord adapter", settings.discord_configured),
        _static(
            "scheduler",
            True if settings.scheduler_enabled else None,
            (
                f"enabled on {settings.primary_platform}"
                if settings.scheduler_enabled
                else "disabled"
            ),
        ),
        _static(
            "classifiers / screening key",
            bool(settings.openrouter_api_key),
            (
                "openrouter_api_key set"
                if settings.openrouter_api_key
                else "openrouter_api_key missing — classifiers fail safe, "
                "screening fails OPEN"
            ),
        ),
        _static(
            "shell / workspace",
            True if settings.shell_enabled else None,
            (
                f"shell on, workspace {settings.workspace_dir}"
                if settings.shell_enabled
                else "shell disabled"
            ),
        ),
        _static(
            "web exposure",
            None,
            (
                f"LAN (0.0.0.0:{settings.web_port}) — password required"
                if settings.web_lan_enabled
                else f"localhost only (127.0.0.1:{settings.web_port})"
            ),
        ),
    ]
    for name, enabled, url in sidecars:
        checks.append(
            _static(
                f"{name} sidecar",
                True if enabled else None,
                f"enabled at {url} (configured, not probed)"
                if enabled
                else "disabled",
            )
        )
    return checks
