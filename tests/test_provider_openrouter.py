"""OpenRouter provider: SSE parsing, tool-call assembly, errors, payload."""

import json
from typing import Any

import httpx
import pytest

from chief.provider.base import Completion, ProviderError, TextDelta, ToolSpec
from chief.provider.openrouter import OpenRouterProvider


def sse_body(chunks: list[dict[str, Any]]) -> str:
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )


def make_provider(
    chunks: list[dict[str, Any]], requests: list[httpx.Request] | None = None
) -> OpenRouterProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            200, text=sse_body(chunks), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterProvider("test-key", client=client)


def delta_chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"delta": delta}]}


async def collect(provider: OpenRouterProvider) -> list[Any]:
    events = []
    async for event in provider.stream(model="m", messages=[], tools=[]):
        events.append(event)
    return events


async def test_streams_text_deltas_and_final_completion() -> None:
    provider = make_provider(
        [
            delta_chunk({"content": "Hel"}),
            delta_chunk({"content": "lo"}),
            {
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.001},
            },
        ]
    )
    events = await collect(provider)
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.text == "Hello"
    assert final.usage.prompt_tokens == 5
    assert final.usage.cost == 0.001


async def test_assembles_tool_calls_streamed_in_fragments() -> None:
    provider = make_provider(
        [
            delta_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "echo", "arguments": '{"te'},
                        }
                    ]
                }
            ),
            delta_chunk(
                {"tool_calls": [{"index": 0, "function": {"arguments": 'xt": "hi"}'}}]}
            ),
        ]
    )
    events = await collect(provider)
    final = events[-1]
    assert isinstance(final, Completion)
    assert len(final.tool_calls) == 1
    call = final.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "echo", {"text": "hi"})


async def test_malformed_tool_arguments_are_surfaced_not_crashed() -> None:
    provider = make_provider(
        [
            delta_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "echo", "arguments": "{broken"},
                        }
                    ]
                }
            )
        ]
    )
    events = await collect(provider)
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.tool_calls[0].arguments == {"_unparsed_arguments": "{broken"}


async def test_http_error_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="insufficient credits")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider("test-key", client=client)
    with pytest.raises(ProviderError, match="402.*insufficient credits"):
        await collect(provider)


async def test_request_payload_carries_tools_auth_and_usage() -> None:
    requests: list[httpx.Request] = []
    provider = make_provider([delta_chunk({"content": "ok"})], requests)
    spec = ToolSpec(name="echo", description="Echo.", parameters={"type": "object"})
    async for _ in provider.stream(
        model="anthropic/claude-sonnet-4.5",
        messages=[{"role": "user", "content": "hi"}],
        tools=[spec],
    ):
        pass
    request = requests[0]
    assert request.headers["authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "anthropic/claude-sonnet-4.5"
    assert payload["stream"] is True
    assert payload["usage"] == {"include": True}
    assert payload["tools"][0]["function"]["name"] == "echo"
