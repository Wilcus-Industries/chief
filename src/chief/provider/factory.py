"""Assemble the LLM provider from config: one backend or a routed set."""

from chief.config import BackendSpec, Config
from chief.provider.anthropic import AnthropicProvider
from chief.provider.base import Provider
from chief.provider.openrouter import OpenRouterProvider
from chief.provider.router import RouterProvider


def build_provider(config: Config) -> Provider:
    """The LLM provider: a RouterProvider over named backends when any are
    configured, else the single legacy OpenRouter/default backend.

    Each backend is an ``OpenRouterProvider`` (``kind: openai``, any
    OpenAI-compatible endpoint) or an ``AnthropicProvider`` (``kind:
    anthropic``), with its own base_url + key. Fails LOUD at boot (not on the
    first turn) if an alias names a backend that was never assembled.
    """
    backends = {
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


def _backend(spec: BackendSpec, temperature: float) -> Provider:
    if spec.kind == "anthropic":
        return AnthropicProvider(spec.api_key, base_url=spec.base_url)
    return OpenRouterProvider(
        spec.api_key, temperature=temperature, base_url=spec.base_url
    )
