"""Assemble the daemon's LLM provider from config (the boot-time factory)."""

from chief.config import BackendSpec, Config
from chief.provider.base import Provider
from chief.provider.openrouter import OpenRouterProvider
from chief.provider.router import RouterProvider


def build_provider(config: Config) -> Provider:
    """The LLM provider: a RouterProvider over named backends when any are
    configured, else the single legacy OpenRouter/default backend.

    Every backend is an OpenAI-compatible ``OpenRouterProvider`` with its own
    base_url + key + sampling options. Fails LOUD at boot (not on the first
    turn) if an alias names a backend that was never assembled.
    """
    backends: dict[str, Provider] = {
        name: _backend(spec, config.temperature)
        for name, spec in config.provider_backends.items()
    }
    default = backends.get("default") or OpenRouterProvider(
        config.openrouter_api_key,
        temperature=config.temperature,
        base_url=config.provider_base_url,
    )
    if not config.provider_backends and not config.provider_aliases:
        return default
    backends.setdefault("default", default)
    aliases: dict[str, tuple[str, str]] = {}
    for name, alias in config.provider_aliases.items():
        if alias.backend not in backends:
            raise ValueError(
                f"provider alias '{name}' names unknown backend '{alias.backend}'"
            )
        aliases[name] = (alias.backend, alias.model)
    return RouterProvider(default=default, backends=backends, aliases=aliases)


def _backend(spec: BackendSpec, temperature: float) -> OpenRouterProvider:
    return OpenRouterProvider(
        spec.api_key,
        temperature=temperature if spec.send_temperature else None,
        reasoning_effort=spec.reasoning_effort,
        base_url=spec.base_url,
    )
