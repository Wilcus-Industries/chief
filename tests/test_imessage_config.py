"""iMessage settings gate, doctor probe, and boot wiring (#156)."""

import sys
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chief.adapters.imessage import (
    REQUIRED_CAPABILITIES,
    IMessageAdapter,
    imessage_ready,
)
from chief.app import build_stacks
from chief.client_plane import SocketServer
from chief.config import Settings, _overlay_denied
from chief.gate.policy import PolicyStore
from chief.memory.markdown_backend import MarkdownMemory
from chief.memory.versioning import NullVersioner
from chief.obs.audit import AuditLog
from chief.tools.apple.doctor import (
    CAPABILITIES,
    STATUS_DENIED,
    STATUS_OK,
    CapabilityHealth,
)

_KW: dict[str, Any] = {"telegram_bot_token": "t", "owner_telegram_id": 1}


# ---- settings gate -----------------------------------------------------------------


def test_imessage_defaults_off_and_needs_the_apple_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(**_KW)
    assert settings.imessage_enabled is False  # opt-in, unlike apple_enabled

    enabled = Settings(
        **_KW, imessage_enabled=True, imessage_owner_handles=("+15550000001",)
    )
    monkeypatch.setattr(sys, "platform", "linux")
    assert enabled.imessage_configured is False  # inert off macOS
    monkeypatch.setattr(sys, "platform", "darwin")
    assert enabled.imessage_configured is True
    # Force-off through the Apple family kills the adapter too (it rides the
    # family's store read layer).
    forced = Settings(
        **_KW,
        imessage_enabled=True,
        imessage_owner_handles=("+15550000001",),
        apple_enabled=False,
    )
    assert forced.imessage_configured is False


def test_imessage_enabled_requires_owner_handles() -> None:
    with pytest.raises(ValueError, match="imessage_owner_handles"):
        Settings(**_KW, imessage_enabled=True)
    with pytest.raises(ValueError, match="imessage_owner_handles"):
        Settings(**_KW, imessage_enabled=True, imessage_owner_handles=(" ",))


def test_poll_seconds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="imessage_poll_seconds"):
        Settings(**_KW, imessage_poll_seconds=0)


def test_self_dm_defaults_off() -> None:
    assert Settings(**_KW).imessage_self_dm is False  # opt-in behavior flag


def test_self_dm_requires_owner_handles() -> None:
    # The self-chat *is* an owner handle, so self-DM needs at least one configured.
    with pytest.raises(ValueError, match="imessage_owner_handles"):
        Settings(**_KW, imessage_self_dm=True)


def test_owner_handles_are_overlay_denied_but_cadence_is_merge_safe() -> None:
    # The whitelist's owner tier is an owner-identity key — chief's own overlay
    # must never mint one (#107 fence); the poll cadence is plain behavior.
    assert _overlay_denied("imessage_owner_handles")
    assert _overlay_denied("imessage_enabled")  # via *_enabled
    assert not _overlay_denied("imessage_poll_seconds")
    # Self-DM is behavior-only (mints no privilege) → agent-editable, not denied.
    assert not _overlay_denied("imessage_self_dm")


# ---- doctor probe ------------------------------------------------------------------


def test_messages_send_is_a_probed_capability() -> None:
    assert "messages_send" in CAPABILITIES


def _health(**status: str) -> list[CapabilityHealth]:
    return [
        CapabilityHealth(
            cap,
            "grant",
            status.get(cap, STATUS_OK),
            "detail",
            "fix steps" if status.get(cap) else "",
        )
        for cap in CAPABILITIES
    ]


def test_imessage_ready_requires_both_probes_green() -> None:
    ok, reason = imessage_ready(_health())
    assert ok and reason == ""

    ok, reason = imessage_ready(_health(messages=STATUS_DENIED))
    assert not ok and "messages" in reason

    ok, reason = imessage_ready(_health(messages_send=STATUS_DENIED))
    assert not ok and "messages_send" in reason

    ok, reason = imessage_ready([])  # no probes ran at all (Linux)
    assert not ok
    assert all(name in reason for name in REQUIRED_CAPABILITIES)


# ---- boot wiring -------------------------------------------------------------------


def _boot_deps(
    tmp_path: Any, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, Any]:
    audit = AuditLog(tmp_path / "audit.jsonl")
    return {
        "socket_server": SocketServer(str(tmp_path / "boot.sock")),
        "session_factory": session_factory,
        "policy": PolicyStore(session_factory, audit=audit),
        "audit": audit,
        "memory": MarkdownMemory(
            str(tmp_path / "memory"),
            versioner=NullVersioner(),
            owner_name="tester",
        ),
    }


def _imessage_settings() -> Settings:
    return Settings(
        **_KW,
        imessage_enabled=True,
        imessage_owner_handles=("+15550000001",),
    )


async def test_build_stacks_adds_the_imessage_stack_when_probes_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    stacks = build_stacks(
        _imessage_settings(),
        apple_health=_health(),
        **_boot_deps(tmp_path, session_factory),
    )

    platforms = [manager.platform for manager, _, _ in stacks]
    assert platforms == ["telegram", "imessage", "cli"]
    adapter = stacks[1][1]
    assert isinstance(adapter, IMessageAdapter)
    for manager, _, _ in stacks:
        await manager.shutdown()


async def test_build_stacks_skips_imessage_when_a_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    stacks = build_stacks(
        _imessage_settings(),
        apple_health=_health(messages_send=STATUS_DENIED),
        **_boot_deps(tmp_path, session_factory),
    )

    platforms = [manager.platform for manager, _, _ in stacks]
    assert platforms == ["telegram", "cli"]  # off, and the boot log says why
    for manager, _, _ in stacks:
        await manager.shutdown()


async def test_build_stacks_is_inert_on_linux(
    tmp_path: Any, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    stacks = build_stacks(
        _imessage_settings(),
        apple_health=[],
        **_boot_deps(tmp_path, session_factory),
    )

    platforms = [manager.platform for manager, _, _ in stacks]
    assert "imessage" not in platforms
    for manager, _, _ in stacks:
        await manager.shutdown()


def test_scheduler_accepts_imessage_as_primary_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    settings = Settings(
        **_KW,
        imessage_enabled=True,
        imessage_owner_handles=("+15550000001",),
        scheduler_enabled=True,
        primary_platform="imessage",
        primary_thread_key="+15550000001",
    )
    assert settings.imessage_configured
