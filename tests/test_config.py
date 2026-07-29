"""Config loading: defaults, yaml, env overrides, secret file fallback, and the
deterministic config-writer (``merge_config`` / ``config_apply``)."""

from pathlib import Path

import pytest
import yaml

from chief.config import (
    AliasSpec,
    ConfigError,
    load_config,
    load_raw,
    merge_config,
)
from chief.config_apply import main as config_apply_main


def test_defaults_when_no_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.default_model
    assert config.max_concurrent_sessions == 4


def test_shell_timeout_defaults_to_20s(tmp_path: Path) -> None:
    # A short default keeps a hung command from stalling the daemon for minutes; the
    # agent can still pass a larger per-call `timeout` for genuinely slow work.
    config = load_config(tmp_path / "missing.yaml")
    assert config.shell_timeout_seconds == 20.0


def test_shell_timeout_yaml_override(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("shell:\n  timeout_seconds: 45\n")
    assert load_config(path).shell_timeout_seconds == 45.0


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


def test_owner_handles_bare_numeric_scalar_raises_clear_error(
    tmp_path: Path,
) -> None:
    """The exact mini boot-loop: a bare ``+1...`` handle parses as a YAML int
    (dropping the ``+``). Old code hit ``tuple(int)`` and crashed deep in boot.
    Coercing it would scope to the wrong chat, so raise an actionable error.
    """
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  owner_handles: +15551234567\n")
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


def test_hooks_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.hooks_timeout_seconds == 10.0
    assert config.hooks_disabled == ()


def test_hooks_yaml_override(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hooks:\n  timeout_seconds: 3\n  disabled: [foo]\n")
    config = load_config(path)
    assert config.hooks_timeout_seconds == 3.0
    assert config.hooks_disabled == ("foo",)


def test_compaction_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.yaml")
    assert config.compaction_ratio == 0.95
    assert config.compaction_keep_recent == 20
    assert config.compaction_default_window == 60_000
    assert config.compaction_windows == {}


def test_compaction_yaml_override(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "compaction:\n"
        "  ratio: 0.8\n"
        "  keep_recent: 10\n"
        "  default_window: 100000\n"
        "  windows:\n"
        "    anthropic/claude-opus-4: 200000\n"
        "    opus: 200000\n"
    )
    config = load_config(path)
    assert config.compaction_ratio == 0.8
    assert config.compaction_keep_recent == 10
    assert config.compaction_default_window == 100_000
    assert config.compaction_windows == {
        "anthropic/claude-opus-4": 200_000,
        "opus": 200_000,
    }


def test_compaction_ratio_out_of_range_raises(tmp_path: Path) -> None:
    for bad in ("0", "1.5", "-0.2"):
        path = tmp_path / "config.yaml"
        path.write_text(f"compaction:\n  ratio: {bad}\n")
        with pytest.raises(ConfigError, match="compaction.ratio"):
            load_config(path)


def test_load_raw_missing_path_is_empty(tmp_path: Path) -> None:
    assert load_raw(tmp_path / "missing.yaml") == {}


def test_load_raw_reads_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("web_port: 9\n")
    assert load_raw(path) == {"web_port": 9}


def test_temperature_defaults_to_zero(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.yaml").temperature == 0.0


def test_temperature_override_and_not_leaked_into_models(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  default: test/model\n  temperature: 0.7\n")
    config = load_config(path)
    assert config.temperature == 0.7
    assert config.default_model == "test/model"
    assert "temperature" not in config.models


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


def test_load_raw_rejects_duplicate_top_level_keys(tmp_path: Path) -> None:
    # PyYAML's silent last-wins let a triple-appended config block pass every
    # check in prod (audit H5) — the strict loader fails the second copy.
    path = tmp_path / "config.yaml"
    path.write_text(
        "obsidian_memory:\n  ambient_n: 3\n"
        "obsidian_memory:\n  ambient_n: 3\n"
    )
    with pytest.raises(ConfigError, match="duplicate key 'obsidian_memory'"):
        load_raw(path)


def test_load_raw_rejects_duplicate_nested_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("models:\n  default: a\n  default: b\n")
    with pytest.raises(ConfigError, match="duplicate key 'default'"):
        load_raw(path)


def test_merge_config_refuses_a_duplicated_base(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("block: {a: 1}\nblock: {a: 2}\n")
    with pytest.raises(ConfigError, match="duplicate key"):
        merge_config({"other": True}, path)


def test_update_autonomy_defaults_to_asking_before_resolving(tmp_path: Path) -> None:
    config = load_config(tmp_path / "none.yaml")
    assert config.update_autonomy == "clean-only"
    assert config.update_schedule == ""


def test_update_autonomy_accepts_the_three_values(tmp_path: Path) -> None:
    for value in ("off", "clean-only", "full"):
        path = tmp_path / f"{value}.yaml"
        path.write_text(
            f"update:\n  autonomy: '{value}'\n  schedule: '0 9 * * *'\n"
        )
        config = load_config(path)
        assert config.update_autonomy == value
        assert config.update_schedule == "0 9 * * *"


def test_bare_off_survives_yamls_boolean_reading(tmp_path: Path) -> None:
    """YAML 1.1 turns a bare `off` into False — which is what was meant."""
    path = tmp_path / "config.yaml"
    path.write_text("update:\n  autonomy: off\n")
    assert load_config(path).update_autonomy == "off"
    path.write_text("update:\n  autonomy: on\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_imessage_mode_defaults_to_todays_self_dm_posture(tmp_path: Path) -> None:
    """Existing installs must be untouched by the dedicated-account work."""
    assert load_config(tmp_path / "none.yaml").imessage_mode == "self"
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  mode: dedicated\n")
    assert load_config(path).imessage_mode == "dedicated"


def test_a_typo_in_imessage_mode_is_refused_not_coerced(tmp_path: Path) -> None:
    """Reading a typo as `self` would leave the echo machinery on for a chief
    that has its own Apple ID — 🤖-stamped replies and a scope for a chat that
    isn't its own."""
    path = tmp_path / "config.yaml"
    path.write_text("imessage:\n  mode: dedicted\n")
    with pytest.raises(ConfigError, match="imessage.mode"):
        load_config(path)


def test_reaching_the_owners_store_without_self_handles_is_refused(
    tmp_path: Path,
) -> None:
    """`self_handles` is the only thing stopping the echo loop once chief also
    polls the owner's store: chief's replies land there as ordinary inbound
    rows and dedicated mode has already turned BOT_PREFIX off. "Set both or
    neither" is documented — fail closed on it like `imessage.mode` does."""
    path = tmp_path / "config.yaml"
    both = "imessage:\n  mode: dedicated\n  owner_db_path: /o/chat.db\n"
    path.write_text(both)
    with pytest.raises(ConfigError, match="self_handles"):
        load_config(path)
    path.write_text(both + "  self_handles: ['chief@example.com']\n")
    assert load_config(path).imessage_self_handles == ("chief@example.com",)
    # Neither is fine, and `self` mode never reaches the owner's store at all.
    path.write_text("imessage:\n  mode: dedicated\n")
    assert load_config(path).imessage_owner_db_path is None
    path.write_text("imessage:\n  mode: self\n  owner_db_path: /o/chat.db\n")
    assert load_config(path).imessage_owner_db_path == Path("/o/chat.db")


def test_a_typo_in_update_autonomy_is_refused_not_coerced(tmp_path: Path) -> None:
    """Neither "silently off" nor "silently on" is an acceptable reading of a
    typo in the key that governs unattended conflict resolution."""
    path = tmp_path / "config.yaml"
    path.write_text("update:\n  autonomy: yes-please\n")
    with pytest.raises(ConfigError):
        load_config(path)
