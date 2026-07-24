"""StreamPolicy: value type, resolution, config coercion, and store round-trip."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from chief.config import coerce
from chief.config.schema import ConfigError
from chief.persistence.db import make_session_factory
from chief.persistence.store import MessageStore
from chief.policy import (
    COARSE,
    RICH,
    StreamPolicy,
    guard_audience,
    resolve,
)

_DEFAULTS = {"imessage": RICH, "web": RICH}
_OWNER = ("+15550009999",)


def _guard(channel: str, thread: str, override: dict[str, object] | None) -> bool:
    return guard_audience(channel, thread, override, _DEFAULTS, _OWNER)


def test_guard_audience_internal_channels_are_never_guarded() -> None:
    assert _guard("web", "web:main", None) is False
    assert _guard("cli", "cli:t", None) is False


def test_guard_audience_self_dm_is_never_guarded() -> None:
    # thread_key is the owner handle, +/case variant still matches.
    assert _guard("imessage", "+15550009999", None) is False
    assert _guard("imessage", "15550009999", None) is False


def test_guard_audience_external_imessage_with_rich_default_is_guarded() -> None:
    assert _guard("imessage", "+15551234567", None) is True


def test_guard_audience_override_can_clear_the_guard() -> None:
    off: dict[str, object] = {"send_guard": False}
    assert _guard("imessage", "+15551234567", off) is False


def test_guard_audience_unknown_external_channel_fails_closed() -> None:
    # A package-added channel (signal/whatsapp) with no channel_defaults entry
    # and no override must guard by default — never silently sendable.
    assert _guard("signal", "+15551234567", None) is True


def test_guard_audience_unknown_external_channel_override_still_wins() -> None:
    off: dict[str, object] = {"send_guard": False}
    assert _guard("signal", "+15551234567", off) is False


def test_from_dict_fills_missing_from_field_defaults() -> None:
    p = StreamPolicy.from_dict({"deltas": True})
    assert p == StreamPolicy(deltas=True, tools=False, results="off", send_guard=False)


def test_from_dict_fills_missing_from_base() -> None:
    p = StreamPolicy.from_dict({"deltas": False}, base=RICH)
    # deltas overridden, the rest inherited from RICH.
    assert p == StreamPolicy(deltas=False, tools=True, results="lazy", send_guard=True)


def test_from_dict_rejects_unknown_results_mode() -> None:
    with pytest.raises(ValueError, match="results"):
        StreamPolicy.from_dict({"results": "sometimes"})


def test_to_dict_round_trips() -> None:
    assert StreamPolicy.from_dict(RICH.to_dict()) == RICH


def test_resolve_override_wins_over_channel_default() -> None:
    got = resolve("imessage", COARSE.to_dict(), {"imessage": RICH})
    assert got == COARSE


def test_resolve_known_channel_uses_its_default() -> None:
    assert resolve("web", None, {"web": RICH}) == RICH


def test_resolve_unknown_channel_is_coarse() -> None:
    assert resolve("cli", None, {"web": RICH}) == COARSE


def test_coerce_defaults_when_absent() -> None:
    got = coerce.stream_channel_defaults(None)
    assert got == {"imessage": RICH, "web": RICH}


def test_coerce_parses_a_yaml_shaped_map() -> None:
    got = coerce.stream_channel_defaults(
        {"cli": {"deltas": True, "tools": True, "results": "inline"}}
    )
    assert got == {
        "cli": StreamPolicy(deltas=True, tools=True, results="inline")
    }


def test_coerce_rejects_a_bad_results_mode_naming_the_channel() -> None:
    with pytest.raises(ConfigError, match="stream.channel_defaults.cli"):
        coerce.stream_channel_defaults({"cli": {"results": "nope"}})


async def test_store_round_trips_the_override(
    engine: AsyncEngine, store: MessageStore
) -> None:
    await store.ensure_session("cli:t", "cli", stream_policy=RICH.to_dict())
    assert await store.stream_policy("cli:t") == RICH.to_dict()
    await store.set_stream_policy("cli:t", COARSE.to_dict())
    assert await store.stream_policy("cli:t") == COARSE.to_dict()


async def test_persisted_override_survives_a_fresh_store(
    engine: AsyncEngine, store: MessageStore
) -> None:
    """AC3: a fresh MessageStore on the same engine still reads the override."""
    await store.ensure_session("cli:t", "cli")
    await store.set_stream_policy("cli:t", RICH.to_dict())
    fresh = MessageStore(make_session_factory(engine))
    stored = await fresh.stream_policy("cli:t")
    assert stored is not None
    assert StreamPolicy.from_dict(stored) == RICH
