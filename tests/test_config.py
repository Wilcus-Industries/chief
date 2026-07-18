"""Config loading: defaults, yaml, env overrides, secret file fallback, and the
deterministic config-writer (``merge_config`` / ``config_apply``)."""

from pathlib import Path

import pytest
import yaml

from chief.config import AliasSpec, load_config, merge_config
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


def test_provider_base_url_defaults_to_openrouter(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.provider_base_url == "https://openrouter.ai/api/v1"


def test_provider_base_url_override_for_local_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pointing the base_url at a local OpenAI-compatible server (e.g. the
    # claude-code-openai-server subscription proxy) is the whole integration.
    path = tmp_path / "config.yaml"
    path.write_text("provider_base_url: http://127.0.0.1:8000/v1\n")
    config = load_config(path)
    assert config.provider_base_url == "http://127.0.0.1:8000/v1"
    monkeypatch.setenv("CHIEF_PROVIDER_BASE_URL", "http://127.0.0.1:9999/v1")
    assert load_config(path).provider_base_url == "http://127.0.0.1:9999/v1"


def test_extra_model_roles_survive_alongside_default(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  default: test/model\n  compaction: test/cheap\n")
    config = load_config(path)
    assert config.models["compaction"] == "test/cheap"
    assert config.default_model == "test/model"


def test_provider_backends_and_aliases_default_empty(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.provider_backends == {}
    assert config.provider_aliases == {}


def _write_routing_config(path: Path) -> None:
    path.write_text(
        "provider_backends:\n"
        "  proxy:\n"
        "    base_url: http://127.0.0.1:8000/v1\n"
        "    api_key_env: PROXY_KEY\n"
        "    api_key_secret: proxy_api_key\n"
        "provider_aliases:\n"
        "  opus: {backend: proxy, model: claude-opus-4-8}\n"
    )


def test_provider_backend_key_resolves_secret_then_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "proxy_api_key").write_text("from-secret\n")
    _write_routing_config(tmp_path / "config.yaml")

    config = load_config(tmp_path / "config.yaml")
    backend = config.provider_backends["proxy"]
    assert backend.base_url == "http://127.0.0.1:8000/v1"
    # Env var unset -> falls back to the secret file.
    assert backend.api_key == "from-secret"
    assert config.provider_aliases["opus"] == AliasSpec(
        backend="proxy", model="claude-opus-4-8"
    )

    # Env var, when set, wins over the secret file.
    monkeypatch.setenv("PROXY_KEY", "from-env")
    reloaded = load_config(tmp_path / "config.yaml")
    assert reloaded.provider_backends["proxy"].api_key == "from-env"


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
