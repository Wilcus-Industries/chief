"""Core configuration: a small flat set of keys from config.yaml, env-overridable.

Split by concern — the schema (:mod:`chief.config.schema`), the load path
(:mod:`chief.config.load`), and the writer (:mod:`chief.config.write`) — with
the public surface re-exported here so callers keep importing from
``chief.config``.
"""

from chief.config.load import load_config, load_raw
from chief.config.schema import AliasSpec, BackendSpec, Config, ConfigError
from chief.config.write import merge_config

__all__ = [
    "AliasSpec",
    "BackendSpec",
    "Config",
    "ConfigError",
    "load_config",
    "load_raw",
    "merge_config",
]
