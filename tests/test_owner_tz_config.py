"""Coverage for the Calendar server's owner_tz resolution.

``docker/mcp-calendar/owner_tz.py`` lives in a vendored image (excluded from the package
and the venv), so we load it by path — ``server.py`` itself can't be imported (its
module-load Google auth + heavy deps aren't in the venv). The resolver bridges chief's
single source of truth (``config.yaml``, which the standalone container reads from a
mount) to the container, with an OWNER_TZ env override and a UTC fallback. An unresolved
timezone silently mis-stamps every booking, so it must be tested.
"""

import importlib.util
from pathlib import Path

import pytest

_TZ_PATH = (
    Path(__file__).resolve().parents[1] / "docker" / "mcp-calendar" / "owner_tz.py"
)
_spec = importlib.util.spec_from_file_location("owner_tz", _TZ_PATH)
assert _spec and _spec.loader
owner_tz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(owner_tz)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("owner_tz: America/Los_Angeles\n", "America/Los_Angeles"),
        ("owner_tz: America/Los_Angeles   # IANA tz\n", "America/Los_Angeles"),
        ("owner_tz: UTC# no space before comment\n", "UTC"),
        ("owner_tz: 'America/New_York'\n", "America/New_York"),
        ('owner_tz: "UTC"\n', "UTC"),
        ("other: 1\nowner_tz: Europe/Paris\n", "Europe/Paris"),
        ("  owner_tz: America/Denver\n", None),  # indented → not the top-level key
        ("calendar_enabled: true\n", None),  # key absent
        ("owner_tz:\n", None),  # present but empty
    ],
)
def test_owner_tz_from_config(
    tmp_path: Path, contents: str, expected: str | None
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(contents, encoding="utf-8")
    assert owner_tz.owner_tz_from_config(str(cfg)) == expected


def test_owner_tz_from_config_missing_file() -> None:
    assert owner_tz.owner_tz_from_config("/no/such/config.yaml") is None


def test_resolve_env_overrides_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("owner_tz: America/Los_Angeles\n", encoding="utf-8")
    assert owner_tz.resolve_owner_tz("Europe/London", str(cfg)) == "Europe/London"


def test_resolve_falls_back_to_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("owner_tz: America/Los_Angeles\n", encoding="utf-8")
    # Empty / None env → use the config value.
    assert owner_tz.resolve_owner_tz("", str(cfg)) == "America/Los_Angeles"
    assert owner_tz.resolve_owner_tz(None, str(cfg)) == "America/Los_Angeles"


def test_resolve_defaults_to_utc(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("calendar_enabled: true\n", encoding="utf-8")  # no owner_tz
    assert owner_tz.resolve_owner_tz(None, str(cfg)) == "UTC"


@pytest.mark.parametrize("bad", ["Not/AZone", "America/Los_Angeles  extra"])
def test_resolve_invalid_zone_falls_back_to_utc(tmp_path: Path, bad: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"owner_tz: {bad}\n", encoding="utf-8")
    # Invalid from either source → UTC (a typo can't crash module load).
    assert owner_tz.resolve_owner_tz(bad, str(cfg)) == "UTC"
    assert owner_tz.resolve_owner_tz(None, str(cfg)) == "UTC"
