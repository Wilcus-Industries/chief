"""Config loading: defaults, yaml, env overrides, secret file fallback, and the
deterministic config-writer (``merge_config`` / ``config_apply``)."""

from pathlib import Path

import pytest
import yaml

from chief.config import load_config, merge_config
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


def test_default_classifier_role_survives_alongside_default(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "models:\n  default: test/model\n  default_classifier: test/cheap\n"
    )
    config = load_config(path)
    assert config.models["default_classifier"] == "test/cheap"
    assert config.default_model == "test/model"


def test_classifiers_dir_default_and_override(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.yaml").classifiers_dir == Path(
        "classifiers"
    )
    path = tmp_path / "config.yaml"
    path.write_text("classifiers_dir: custom/dir\n")
    assert load_config(path).classifiers_dir == Path("custom/dir")


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
