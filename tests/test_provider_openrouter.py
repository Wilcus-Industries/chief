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


async def test_connection_error_wraps_as_loud_provider_error() -> None:
    # A down backend must fail LOUD as a ProviderError carrying base_url/model
    # context — never a raw httpx error that leaks past the dispatch handler.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider(
        "k", base_url="http://127.0.0.1:8000/v1", client=client, retry_backoff=0.0
    )
    with pytest.raises(ProviderError, match="unreachable.*8000"):
        await collect(provider)


class _RaisingStream(httpx.AsyncByteStream):
    """A response body that yields some bytes, then drops mid-stream."""

    def __init__(self, chunks: list[bytes], exc: BaseException) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk
        raise self._exc

    async def aclose(self) -> None:
        pass


def _flaky_provider(
    fail_times: int, exc: BaseException, chunks: list[dict[str, Any]]
) -> OpenRouterProvider:
    """Provider whose backend raises `exc` on the first `fail_times` calls
    (before sending any body), then streams `chunks` successfully."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return httpx.Response(
            200, text=sse_body(chunks), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterProvider("k", client=client, retry_backoff=0.0)


async def test_transient_drop_before_output_is_retried_then_succeeds() -> None:
    # A single mid-connect transport blip must be retried, not surfaced — the
    # proxy occasionally resets a stream under load and one turn shouldn't die.
    provider = _flaky_provider(
        fail_times=1,
        exc=httpx.ReadError("connection reset"),
        chunks=[delta_chunk({"content": "hi"})],
    )
    events = await collect(provider)
    assert events[0] == TextDelta("hi")
    assert isinstance(events[-1], Completion)


async def test_retry_exhausted_names_the_underlying_exception_type() -> None:
    # When retries run out the loud error must name the real cause (a dropped
    # stream is a ReadError, NOT literally "unreachable") so it is diagnosable.
    provider = _flaky_provider(
        fail_times=99, exc=httpx.ReadError("reset"), chunks=[]
    )
    with pytest.raises(ProviderError, match="ReadError"):
        await collect(provider)


async def test_drop_after_first_delta_is_not_retried() -> None:
    # Once output has been yielded a retry would double-emit — must fail loud.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = b'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
        return httpx.Response(
            200,
            stream=_RaisingStream([body], httpx.RemoteProtocolError("peer closed")),
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider("k", client=client, retry_backoff=0.0)
    events: list[Any] = []
    with pytest.raises(ProviderError, match="RemoteProtocolError"):
        async for event in provider.stream(model="m", messages=[], tools=[]):
            events.append(event)
    assert events == [TextDelta("par")]
    assert calls["n"] == 1  # never retried after emitting output


async def test_base_url_override_targets_local_proxy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=sse_body([delta_chunk({"content": "ok"})]),
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider(
        "cci-key", base_url="http://127.0.0.1:8000/v1", client=client
    )
    async for _ in provider.stream(model="claude-sonnet-4.5", messages=[], tools=[]):
        pass
    assert str(requests[0].url) == "http://127.0.0.1:8000/v1/chat/completions"


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
