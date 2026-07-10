"""Chief's self_config.yaml overlay: deep-merges over config.yaml, minus a denylist.

Real files, real ``Settings`` construction, nothing mocked — the overlay source (#107)
is the central mechanism, so every test drives it end to end.
"""

import fnmatch
import logging
from pathlib import Path

import pytest

from chief.config import (
    DEFAULT_SELF_CONFIG_PATH,
    MERGE_SAFE,
    SELF_CONFIG_DENYLIST,
    Settings,
    _overlay_denied,
)
from chief.gate.blacklist import DEFAULT_SHELL_PATTERNS


def _boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base: str = "owner_telegram_id: 42\n",
    overlay: str | None = None,
) -> Settings:
    """Write config.yaml + secrets (+ optional overlay), chdir, build ``Settings``."""
    (tmp_path / "config.yaml").write_text(base)
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "telegram_bot_token").write_text("tg-secret")
    if overlay is not None:
        overlay_file = tmp_path / DEFAULT_SELF_CONFIG_PATH
        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_text(overlay)
    monkeypatch.chdir(tmp_path)
    return Settings(_secrets_dir=str(secrets))  # type: ignore[call-arg]


def test_overlay_beats_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _boot(
        tmp_path,
        monkeypatch,
        base="owner_telegram_id: 42\nturn_timeout_seconds: 100.0\n",
        overlay="turn_timeout_seconds: 55.0\n",
    )
    assert settings.turn_timeout_seconds == 55.0


def test_env_beats_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_TIMEOUT_SECONDS", "222")
    settings = _boot(
        tmp_path,
        monkeypatch,
        base="owner_telegram_id: 42\nturn_timeout_seconds: 100.0\n",
        overlay="turn_timeout_seconds: 55.0\n",
    )
    assert settings.turn_timeout_seconds == 222.0


def test_secret_file_never_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="chief.config"):
        settings = _boot(
            tmp_path, monkeypatch, overlay="telegram_bot_token: stolen\n"
        )
    assert settings.telegram_bot_token == "tg-secret"
    assert "telegram_bot_token" in caplog.text


def test_deep_merge_preserves_sibling_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _boot(
        tmp_path,
        monkeypatch,
        base=(
            "owner_telegram_id: 42\n"
            "routing_surface_defaults:\n  home: writing\n  dm: research\n"
        ),
        overlay="routing_surface_defaults:\n  home: code\n",
    )
    assert settings.routing_surface_defaults == {"home": "code", "dm": "research"}


def test_screening_enabled_flip_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="chief.config"):
        settings = _boot(
            tmp_path, monkeypatch, overlay="screening_enabled: false\n"
        )
    assert settings.screening_enabled is True
    assert "screening_enabled" in caplog.text


@pytest.mark.parametrize(
    ("overlay", "attr", "expected"),
    [
        ("blacklist_shell_patterns: []\n", "blacklist_shell_patterns",
         DEFAULT_SHELL_PATTERNS),
        ("never_seed:\n  - tool: Bash\n", "never_seed", []),
        ("owner_discord_id: 777\n", "owner_discord_id", None),
        ("db_path: /tmp/evil.db\n", "db_path", "data/chief.db"),
        ("gmail_mcp_url: http://evil/mcp\n", "gmail_mcp_url",
         "http://127.0.0.1:8004/mcp"),
        ("memory_git: false\n", "memory_git", True),
        ("harness_git: false\n", "harness_git", True),
        ("memory_dir: /tmp/evil\n", "memory_dir", "data/memory"),
        ("harness_dir: /tmp/evil\n", "harness_dir", "data/harness"),
        ("subagents_dir: /tmp/evil\n", "subagents_dir", "data/harness/subagents"),
        ("chief_skills_dir: /tmp/evil\n", "chief_skills_dir", "data/harness/skills"),
        # The audit log is one of the three controls left after the sandbox went away
        # (blacklist + screening + audit log). Repointing it defeats forensics exactly
        # as flipping harness_git off defeats revert, so it is denied too (#120).
        ("audit_log_path: /tmp/evil.jsonl\n", "audit_log_path", "data/audit.jsonl"),
        ("shell_enabled: true\n", "shell_enabled", False),
        ("front_desk_thread_key: 'telegram:1'\n", "front_desk_thread_key", None),
        ("self_config_path: elsewhere.yaml\n", "self_config_path",
         DEFAULT_SELF_CONFIG_PATH),
    ],
)
def test_denylist_families_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlay: str,
    attr: str,
    expected: object,
) -> None:
    settings = _boot(tmp_path, monkeypatch, overlay=overlay)
    assert getattr(settings, attr) == expected


def test_harness_git_dropped_and_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # harness_git is a real Settings field (#110); the logged drop proves the
    # denylist still catches it by name — chief cannot switch off its own
    # reversibility, same as memory_git.
    with caplog.at_level(logging.WARNING, logger="chief.config"):
        settings = _boot(tmp_path, monkeypatch, overlay="harness_git: false\n")
    assert settings.owner_telegram_id == 42  # boot succeeded
    assert "harness_git" in caplog.text
    assert settings.harness_git is True


def test_absent_empty_broken_overlay_boot_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for overlay in (None, ""):
        settings = _boot(tmp_path, monkeypatch, overlay=overlay)
        assert settings.turn_timeout_seconds == 300.0

    with caplog.at_level(logging.WARNING, logger="chief.config"):
        settings = _boot(tmp_path, monkeypatch, overlay='{broken: [\n')
    assert settings.turn_timeout_seconds == 300.0
    assert "self_config" in caplog.text


def test_non_string_keys_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # YAML 1.1: unquoted ``on`` parses to bool True, ``42`` to int — neither may
    # reach pydantic-settings as a kwarg (TypeError: keywords must be strings).
    with caplog.at_level(logging.WARNING, logger="chief.config"):
        settings = _boot(
            tmp_path,
            monkeypatch,
            overlay="on: fast\n42: x\nturn_timeout_seconds: 55.0\n",
        )
    assert settings.turn_timeout_seconds == 55.0  # string keys still apply
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "non-string keys" in message
    assert "True" in message and "42" in message


def test_env_repoints_overlay_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alt_file = tmp_path / "alt_overlay.yaml"
    alt_file.write_text("turn_timeout_seconds: 77.0\n")
    monkeypatch.setenv("SELF_CONFIG_PATH", str(alt_file))
    settings = _boot(
        tmp_path,
        monkeypatch,
        base="owner_telegram_id: 42\nturn_timeout_seconds: 100.0\n",
        overlay=None,
    )
    assert settings.turn_timeout_seconds == 77.0


# --- #109: every Settings field is denied or explicitly merge-safe -----------------
#
# These are pure logic tests over ``SELF_CONFIG_DENYLIST`` / ``MERGE_SAFE`` and
# ``Settings.model_fields`` — no ``_boot``, no tmp_path, no I/O.

#: For each wildcard denylist pattern, the field names it is claimed to cover. This
#: is a lower bound: a test below fails if a claimed field no longer exists or no
#: longer matches its pattern (e.g. a field is renamed out from under it).
_PATTERN_COVERAGE = {
    "blacklist_*": {"blacklist_shell_patterns", "blacklist_tools"},
    "screening_*": {"screening_enabled", "screening_model",
                    "screening_block", "screening_tools"},
    "*_enabled": {
        "routing_enabled", "screening_enabled", "guest_enabled",
        "calendar_enabled", "drive_enabled", "sheets_enabled", "gmail_enabled",
        "playwright_enabled", "shell_enabled", "workspace_enabled",
        "web_tools_enabled", "scheduler_enabled", "budget_enabled",
        "skills_enabled", "subagents_enabled", "group_chat_enabled",
    },
    "owner_*_id": {"owner_telegram_id", "owner_discord_id",
                   "owner_home_chat_id", "owner_home_guild_id"},
    "*_token": {"telegram_bot_token", "discord_bot_token"},
    "*_api_key": {"openrouter_api_key", "brave_search_api_key"},
    "*_mcp_url": {"calendar_mcp_url", "drive_mcp_url", "sheets_mcp_url",
                  "gmail_mcp_url", "playwright_mcp_url"},
    "*_thread_key": {"front_desk_thread_key", "primary_thread_key"},
}


def test_every_settings_field_classified() -> None:
    for name in Settings.model_fields:
        assert _overlay_denied(name) or name in MERGE_SAFE, (
            f"Settings field {name!r} is unclassified: add it to "
            "SELF_CONFIG_DENYLIST if security-sensitive, else to MERGE_SAFE."
        )


def test_denied_and_merge_safe_disjoint() -> None:
    denied = {n for n in Settings.model_fields if _overlay_denied(n)}
    assert denied.isdisjoint(MERGE_SAFE), sorted(denied & MERGE_SAFE)
    assert denied | MERGE_SAFE == set(Settings.model_fields)


def test_no_stale_merge_safe_entries() -> None:
    assert set(MERGE_SAFE) <= set(Settings.model_fields), sorted(
        set(MERGE_SAFE) - set(Settings.model_fields)
    )


def test_denylist_patterns_cover_claimed_fields() -> None:
    wildcard_patterns = {p for p in SELF_CONFIG_DENYLIST if "*" in p or "?" in p}
    assert set(_PATTERN_COVERAGE) == wildcard_patterns, (
        "_PATTERN_COVERAGE is out of sync with SELF_CONFIG_DENYLIST's wildcard "
        f"patterns: coverage={sorted(_PATTERN_COVERAGE)} "
        f"patterns={sorted(wildcard_patterns)}"
    )
    for pattern, claimed in _PATTERN_COVERAGE.items():
        for name in claimed:
            assert name in Settings.model_fields, (
                f"{name!r} claimed by pattern {pattern!r} is no longer a "
                "Settings field"
            )
            assert fnmatch.fnmatchcase(name, pattern), (
                f"{name!r} no longer matches its claimed pattern {pattern!r}"
            )
