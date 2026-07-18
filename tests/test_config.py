"""Config loading: defaults, yaml, env overrides, secret file fallback, and the
deterministic config-writer (``merge_config`` / ``config_apply``)."""

from pathlib import Path

import pytest
import yaml

from chief.config import ConfigError, load_config, merge_config
from chief.config_apply import main as config_apply_main


def test_defaults_when_no_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.default_model
    assert config.max_concurrent_sessions == 4


def test_yaml_values_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "models:\n  default: test/model\n"
        "db_path: /tmp/x.db\nmax_concurrent_sessions: 2\n"
    )
    config = load_config(path)
    assert config.default_model == "test/model"
    assert config.db_path == Path("/tmp/x.db")
    assert config.max_concurrent_sessions == 2


def test_env_overrides_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("db_path: /tmp/from_yaml.db\n")
    monkeypatch.setenv("CHIEF_DB_PATH", "/tmp/from_env.db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    config = load_config(path)
    assert config.db_path == Path("/tmp/from_env.db")
    assert config.openrouter_api_key == "sk-env"


def test_extra_model_roles_survive_alongside_default(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  default: test/model\n  compaction: test/cheap\n")
    config = load_config(path)
    assert config.models["compaction"] == "test/cheap"
    assert config.default_model == "test/model"


def test_owner_handles_bare_numeric_scalar_raises_clear_error(
    tmp_path: Path,
) -> None:
    """The exact mini boot-loop: a bare ``+1...`` handle parses as a YAML int
    (dropping the ``+``). Old code hit ``tuple(int)`` and crashed deep in boot.
    Coercing it would scope to the wrong chat, so raise an actionable error.
    """
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  owner_handles: +16507321162\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_owner_handles_single_string_coerces_to_tuple(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  owner_handles: '+1'\n")
    assert load_config(path).imessage_owner_handles == ("+1",)


def test_owner_handles_list_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  owner_handles:\n    - '+1'\n    - '+2'\n")
    assert load_config(path).imessage_owner_handles == ("+1", "+2")
    empty = tmp_path / "empty.yaml"
    empty.write_text("imessage:\n  enabled: true\n")
    assert load_config(empty).imessage_owner_handles == ()


def test_owner_handles_bad_shape_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  owner_handles:\n    a: 1\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_temperature_defaults_to_zero(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.yaml").temperature == 0.0


def test_temperature_override_and_not_leaked_into_models(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  default: test/model\n  temperature: 0.7\n")
    config = load_config(path)
    assert config.temperature == 0.7
    assert config.default_model == "test/model"
    assert "temperature" not in config.models


def test_merge_config_creates_and_deep_merges(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    merge_config({"imessage": {"enabled": True}}, path)
    merge_config({"imessage": {"owner_handles": ["+1"]}, "web_port": 9}, path)
    loaded = yaml.safe_load(path.read_text())
    assert loaded == {
        "imessage": {"enabled": True, "owner_handles": ["+1"]},
        "web_port": 9,
    }


def test_config_apply_parses_dotted_yaml_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_apply_main(["imessage.enabled=true", 'imessage.owner_handles=["+1","+2"]'])
    loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert loaded == {"imessage": {"enabled": True, "owner_handles": ["+1", "+2"]}}
