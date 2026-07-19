"""Model-name routing across several provider backends.

A single :class:`RouterProvider` fronts one ``default`` backend plus any number
of named extra backends. A typed alias (e.g. ``opus``) maps to
``(backend_name, real_model)``: the turn is delegated to that named backend
with the model id *rewritten* to the backend's real model. Any model not in the
alias table goes to ``default`` unchanged. A backend being unreachable surfaces
as that backend's own :class:`~chief.provider.base.ProviderError` — the router
never silently swaps to a different backend.
"""

from collections.abc import AsyncIterator
from typing import Any

from chief.provider.base import Provider, ProviderEvent, ToolSpec


class RouterProvider:
    """Routes each turn to a backend by model name (see module docstring)."""

    def __init__(
        self,
        *,
        default: Provider,
        backends: dict[str, Provider],
        aliases: dict[str, tuple[str, str]],
    ) -> None:
        self._default = default
        self._backends = backends
        self._aliases = aliases

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        """Route by model name, rewriting the model id for aliased backends."""
        provider, real_model = self._route(model)
        async for event in provider.stream(
            model=real_model, messages=messages, tools=tools
        ):
            yield event

    def _route(self, model: str) -> tuple[Provider, str]:
        alias = self._aliases.get(model)
        if alias is None:
            return self._default, model
        backend_name, real_model = alias
        return self._backends[backend_name], real_model
