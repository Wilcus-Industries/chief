"""Config loading: defaults, yaml, env overrides, secret file fallback."""

from pathlib import Path

import pytest

from chief.config import load_config


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
