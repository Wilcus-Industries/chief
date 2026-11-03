"""Anthropic provider: wire translation, SSE parsing, cost, errors, retry."""

import json
from typing import Any

import httpx
import pytest

from chief.provider.anthropic import AnthropicProvider
from chief.provider.anthropic_wire import to_anthropic_request
from chief.provider.base import Completion, ProviderError, TextDelta, ToolCall, ToolSpec


def sse_body(events: list[dict[str, Any]]) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    )


def text_events(
    *parts: str, input_tokens: int = 10, output_tokens: int = 4
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
    ]
    events += [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": part}}
        for part in parts
    ]
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]
    return events


def make_provider(
    events: list[dict[str, Any]],
    requests: list[httpx.Request] | None = None,
    status: int = 200,
) -> AnthropicProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if status != 200:
            return httpx.Response(status, text='{"error": "nope"}')
        return httpx.Response(
            200, text=sse_body(events), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AnthropicProvider("sk-test", client=client)


async def collect(
    provider: AnthropicProvider,
    model: str = "claude-haiku-5-5",
    messages: list[dict[str, Any]] | None = None,
    tools: list[ToolSpec] | None = None,
) -> list[Any]:
    return [
        event
        async for event in provider.stream(
            model=model,
            messages=messages or [{"role": "user", "content": "hi"}],
            tools=tools or [],
        )
    ]


async def test_streams_text_and_prices_known_model() -> None:
    provider = make_provider(
        text_events("Hel", "lo", input_tokens=1_000_000, output_tokens=1_000_000)
    )
    events = await collect(provider, model="claude-sonnet-5-5")
    assert events[:2] == [TextDelta("Hel"), TextDelta("lo")]
    final = events[-1]
    assert isinstance(final, Completion)
    assert final.text == "Hello"
    assert final.usage.prompt_tokens == 1_000_000
    assert final.usage.completion_tokens == 1_000_000
    assert final.usage.cost == pytest.approx(12.0)  # $2 in + $10 out


async def test_unknown_model_costs_zero() -> None:
    provider = make_provider(text_events("x"))
    final = (await collect(provider, model="claude-future-9"))[-1]
    assert final.usage.cost == 0.0


async def test_sends_auth_headers_and_streaming_body() -> None:
    requests: list[httpx.Request] = []
    provider = make_provider(text_events("ok"), requests)
    await collect(provider)
    request = requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["stream"] is True
    assert body["max_tokens"] > 0


async def test_assembles_streamed_tool_call() -> None:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "echo", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"te'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'xt": "hi"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
    final = (await collect(make_provider(events)))[-1]
    assert final.tool_calls == (ToolCall("toolu_1", "echo", {"text": "hi"}),)


async def test_thinking_blocks_are_not_surfaced_as_text() -> None:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "secret"}},
        {"type": "content_block_stop", "index": 0},
        *text_events("answer")[1:],
    ]
    final = (await collect(make_provider(events)))[-1]
    assert final.text == "answer"


async def test_http_error_raises_provider_error() -> None:
    with pytest.raises(ProviderError, match="Anthropic HTTP 401"):
        await collect(make_provider([], status=401))


async def test_stream_error_event_raises() -> None:
    events = [{"type": "error", "error": {"type": "overloaded_error",
                                          "message": "busy"}}]
    with pytest.raises(ProviderError, match="overloaded_error"):
        await collect(make_provider(events))


async def test_transport_error_before_data_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, text=sse_body(text_events("ok")))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider("k", client=client, retry_backoff=0)
    final = (await collect(provider))[-1]
    assert final.text == "ok"
    assert calls == 2


def test_wire_lifts_system_and_translates_tool_round_trip() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "echo hi"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "toolu_1", "type": "function",
            "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "hi"},
        {"role": "system", "content": "[summary] earlier stuff"},
    ]
    tools = [ToolSpec("echo", "Echo text.", {"type": "object", "properties": {}})]
    body = to_anthropic_request("claude-haiku-5-5", messages, tools, max_tokens=99)
    assert body["system"] == "be terse\n\n[summary] earlier stuff"
    assert body["messages"] == [
        {"role": "user", "content": "echo hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "echo",
             "input": {"text": "hi"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]},
    ]
    assert body["tools"] == [{"name": "echo", "description": "Echo text.",
                              "input_schema": {"type": "object", "properties": {}}}]
    assert body["max_tokens"] == 99


def test_wire_merges_parallel_tool_results_into_one_user_turn() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "on it", "tool_calls": [
            {"id": "a", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    body = to_anthropic_request("m", messages, [], max_tokens=10)
    assert body["messages"][1]["content"][0] == {"type": "text", "text": "on it"}
    assert body["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "1"},
        {"type": "tool_result", "tool_use_id": "b", "content": "2"}]}
    assert "system" not in body
