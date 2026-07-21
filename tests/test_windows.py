"""Per-model context-window resolution (WindowResolver)."""

import httpx
import pytest

from chief.agent.windows import WindowResolver

MODELS_JSON = {
    "data": [
        {"id": "anthropic/claude-opus-4", "context_length": 200_000},
        {"id": "qwen/qwen3-coder", "context_length": 128_000},
        {"id": "broken/no-context"},
    ]
}


def _resolver(
    handler: httpx.MockTransport | None = None,
    *,
    windows: dict[str, int] | None = None,
    aliases: tuple[str, ...] = (),
    default_window: int = 60_000,
) -> WindowResolver:
    client = None
    if handler is not None:
        client = httpx.AsyncClient(transport=handler)
    return WindowResolver(
        windows=windows or {},
        default_window=default_window,
        aliases=aliases,
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        client=client,
    )


def _ok_transport(counter: list[int]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        counter.append(1)
        return httpx.Response(200, json=MODELS_JSON)

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_config_override_wins_without_fetch() -> None:
    calls: list[int] = []
    resolver = _resolver(_ok_transport(calls), windows={"opus": 300_000})
    assert await resolver.resolve("opus") == 300_000
    assert calls == []  # override short-circuits before any network call


@pytest.mark.asyncio
async def test_openrouter_metadata_hit() -> None:
    resolver = _resolver(_ok_transport([]))
    assert await resolver.resolve("anthropic/claude-opus-4") == 200_000


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_default() -> None:
    resolver = _resolver(_ok_transport([]), default_window=42_000)
    assert await resolver.resolve("who/knows") == 42_000


@pytest.mark.asyncio
async def test_entry_without_context_length_is_skipped() -> None:
    resolver = _resolver(_ok_transport([]), default_window=42_000)
    assert await resolver.resolve("broken/no-context") == 42_000


@pytest.mark.asyncio
async def test_alias_uses_default_without_fetch() -> None:
    calls: list[int] = []
    resolver = _resolver(
        _ok_transport(calls), aliases=("opus",), default_window=55_000
    )
    assert await resolver.resolve("opus") == 55_000
    assert calls == []  # a named-backend alias never triggers the OR fetch


@pytest.mark.asyncio
async def test_table_is_fetched_once_and_cached() -> None:
    calls: list[int] = []
    resolver = _resolver(_ok_transport(calls))
    assert await resolver.resolve("qwen/qwen3-coder") == 128_000
    assert await resolver.resolve("anthropic/claude-opus-4") == 200_000
    assert sum(calls) == 1  # both lookups share the one fetched table


@pytest.mark.asyncio
async def test_fetch_failure_falls_back_and_may_retry() -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=MODELS_JSON)

    resolver = _resolver(httpx.MockTransport(handle), default_window=9_000)
    assert await resolver.resolve("qwen/qwen3-coder") == 9_000  # 500 -> default
    assert await resolver.resolve("qwen/qwen3-coder") == 128_000  # retry succeeds
    assert sum(calls) == 2
