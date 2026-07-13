"""Apple family gating + wiring tests (#155).

Covers the settings gate (auto-detected on darwin, force-off, inert on Linux), the
blacklist/screening seeds, the boot resolver, owner-session wiring (servers
registered, tools off the allow-list, guests isolated), and the persona guidance.
"""

import sys
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.app import build_apple_family, resolve_apple_services
from chief.config import _DEFAULT_BLACKLIST_TOOLS, Settings, _overlay_denied
from chief.core.personas import build_system_prompt
from chief.core.tasks import SessionProto, TaskManager
from chief.tools.apple import calendar as apple_calendar
from chief.tools.apple import messages as apple_messages
from chief.tools.apple import reminders as apple_reminders
from chief.tools.apple import shortcuts as apple_shortcuts
from chief.tools.apple.doctor import AppleDoctorService
from chief.tools.apple.runner import ScriptResult, ScriptRunner
from test_browser_wiring import _capture_factory, _FakeIO, _FakeMemory, _no

Factory = Callable[..., SessionProto]

_SETTINGS_KW: dict[str, Any] = {"telegram_bot_token": "t", "owner_telegram_id": 1}


# ---- settings gate ------------------------------------------------------------


def test_apple_enabled_defaults_on_but_only_configures_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(**_SETTINGS_KW)
    assert settings.apple_enabled is True  # auto-detected, not opt-in

    monkeypatch.setattr(sys, "platform", "linux")
    assert settings.apple_configured is False  # silently absent on Linux

    monkeypatch.setattr(sys, "platform", "darwin")
    assert settings.apple_configured is True  # just there on a Mac


def test_apple_force_off_wins_even_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(apple_enabled=False, **_SETTINGS_KW)
    assert settings.apple_configured is False


def test_apple_runner_bounds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(apple_script_timeout_seconds=0, **_SETTINGS_KW)
    with pytest.raises(ValueError):
        Settings(apple_output_limit=-1, **_SETTINGS_KW)


def test_apple_messages_db_path_expands_user() -> None:
    settings = Settings(**_SETTINGS_KW)
    assert not settings.apple_messages_db_path.startswith("~")
    assert settings.apple_messages_db_path.endswith("Library/Messages/chat.db")


def test_apple_keys_are_fenced_from_the_self_config_overlay() -> None:
    # chief must not be able to switch its own Apple surface on/off or repoint
    # which database the Messages tools read (#107 denylist classes).
    assert _overlay_denied("apple_enabled")
    assert _overlay_denied("apple_messages_db_path")
    assert not _overlay_denied("apple_script_timeout_seconds")


# ---- blacklist + screening seeds -------------------------------------------------


def test_run_shortcut_and_apple_calendar_write_are_blacklist_seeded() -> None:
    # PRD #155: mutating tools that reach other people or destroy data ASK. The
    # arbitrary-shortcut escape hatch and the calendar write (mirroring the Google
    # calendar posture) are the family's two gated shapes.
    assert apple_shortcuts.RUN_TOOL in _DEFAULT_BLACKLIST_TOOLS
    assert apple_calendar.CREATE_TOOL in _DEFAULT_BLACKLIST_TOOLS
    # Reads and owner-local creates flow freely.
    assert apple_shortcuts.LIST_TOOL not in _DEFAULT_BLACKLIST_TOOLS
    assert apple_calendar.LIST_TOOL not in _DEFAULT_BLACKLIST_TOOLS
    assert apple_reminders.CREATE_TOOL not in _DEFAULT_BLACKLIST_TOOLS


def test_messages_reads_are_screened_untrusted_channels() -> None:
    # Message text arrives from other people — same screening class as Gmail reads.
    settings = Settings(**_SETTINGS_KW)
    for tool_name in apple_messages.READ_TOOL_NAMES:
        assert tool_name in settings.screening_tools


# ---- boot resolver ------------------------------------------------------------------


def test_build_apple_family_is_none_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert build_apple_family(Settings(**_SETTINGS_KW)) is None


def test_build_apple_family_carries_the_configured_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(
        apple_script_timeout_seconds=7.5, apple_output_limit=1234, **_SETTINGS_KW
    )
    family = build_apple_family(settings)
    assert family is not None
    assert family.runner.timeout == 7.5
    assert family.runner.output_limit == 1234
    assert family.messages_db_path == settings.apple_messages_db_path
    assert family.screenshots_dir == settings.playwright_screenshots_dir


async def test_resolve_apple_services_is_empty_off_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert await resolve_apple_services(Settings(**_SETTINGS_KW)) == ()


async def test_resolve_on_darwin_without_binaries_registers_only_the_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A box that claims darwin but lacks the macOS binaries (this CI runner): every
    # probe reports unavailable, so per-capability gating strips every app area and
    # only the doctor registers — degradation, not a crash.
    monkeypatch.setattr(sys, "platform", "darwin")
    services = await resolve_apple_services(Settings(**_SETTINGS_KW))
    assert [svc.capability for svc in services] == ["doctor"]


# ---- owner-session wiring ------------------------------------------------------------


class _OkRunner(ScriptRunner):
    async def run(
        self, argv: Any, *, stdin: bytes | None = None
    ) -> ScriptResult:
        return ScriptResult("[]", "", 0)


def _apple_manager(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    factory: Factory,
    enabled: bool = True,
) -> TaskManager:
    runner = _OkRunner()
    services = (
        (
            apple_reminders.RemindersService(runner=runner),
            apple_shortcuts.ShortcutsService(runner=runner),
            AppleDoctorService(runner=runner, messages_db_path="/db"),
        )
        if enabled
        else ()
    )
    return TaskManager(
        session_factory=session_factory,
        io=_FakeIO(),
        owner_model="claude-sonnet-4-6",
        classifier_model="claude-haiku-4-5",
        session_factory_sdk=factory,
        stop_intent=_no,
        warrants_task=_no,
        memory=_FakeMemory(),
        memory_dir="/tmp/mem",
        owner_name="Will",
        apple_services=services,
    )


async def test_owner_session_registers_apple_servers_off_the_allow_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _apple_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:5", tier="owner")

    servers = captured["mcp_servers"]
    assert "chief_apple_reminders" in servers
    assert "chief_apple_shortcuts" in servers
    assert "chief_apple_doctor" in servers
    # Every Apple tool stays OFF allowed_tools so calls route through the gate:
    # default-allow ALLOWs the reads, the blacklist cards run_shortcut.
    allowed = set(captured["allowed_tools"])
    assert apple_reminders.CREATE_TOOL not in allowed
    assert apple_shortcuts.RUN_TOOL not in allowed
    # The persona names the healthy capabilities (never the doctor).
    prompt = captured["system_prompt"]
    assert "## Apple" in prompt
    assert "reminders, shortcuts" in prompt
    assert "doctor" not in prompt.split("## Apple")[1].split("##")[0]
    await mgr.shutdown()


async def test_guest_session_never_sees_apple_tools(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _apple_manager(session_factory, factory=_capture_factory(captured))

    await mgr._ensure_task("-100:9", tier="guest")

    assert "mcp_servers" not in captured  # owner-only, tier isolation
    assert captured["allowed_tools"] == []
    assert "## Apple" not in captured["system_prompt"]
    await mgr.shutdown()


async def test_without_apple_services_the_owner_prompt_has_no_apple_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: dict[str, Any] = {}
    mgr = _apple_manager(
        session_factory, factory=_capture_factory(captured), enabled=False
    )

    await mgr._ensure_task("-100:5", tier="owner")

    assert "mcp_servers" not in captured
    assert "## Apple" not in captured["system_prompt"]
    await mgr.shutdown()


# ---- persona guidance ----------------------------------------------------------------


def test_apple_guidance_names_capabilities_and_the_doctor() -> None:
    prompt = build_system_prompt(
        tier="owner",
        memory=_FakeMemory(),
        owner_name="Will",
        apple_capabilities=("reminders", "notes", "messages"),
    )
    assert "## Apple" in prompt
    assert "reminders, notes, messages" in prompt
    assert "check_apple_health" in prompt
    assert "never ask what platform" in prompt


def test_guest_prompt_never_carries_apple_guidance() -> None:
    prompt = build_system_prompt(
        tier="guest",
        memory=_FakeMemory(),
        owner_name="Will",
        apple_capabilities=("reminders",),
    )
    assert "## Apple" not in prompt
