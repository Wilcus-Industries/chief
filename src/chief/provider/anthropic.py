"""Native Anthropic Messages API implementation of the provider seam (SSE).

A peer of :mod:`chief.provider.openrouter`, selected per backend with
``provider_backends.<name>.kind: anthropic``. Thinking blocks are consumed and
dropped: the transcript is OpenAI-shaped, and a harness that strips thinking
gives the API's preserved-thinking check nothing to reject.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from chief.provider.anthropic_wire import cost_usd, to_anthropic_request
from chief.provider.base import (
    Completion,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    parse_arguments,
)
from chief.provider.retry import stream_with_retry

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    """Streams turns from the Anthropic Messages API with an API key."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        max_tokens: int = 16000,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=30)
        )
        self._base_url = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "anthropic-version": _API_VERSION}
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        """Yield TextDeltas as tokens arrive; end with one priced Completion."""
        body = to_anthropic_request(
            model, messages, tools, max_tokens=self._max_tokens
        )
        async for event in stream_with_retry(
            lambda progressed: self._attempt(body, progressed),
            max_retries=self._max_retries,
            backoff=self._retry_backoff,
            where=f"{self._base_url}, model {model}",
        ):
            yield event

    async def _attempt(
        self, body: dict[str, Any], progressed: list[bool]
    ) -> AsyncIterator[ProviderEvent]:
        acc = _Accumulator(str(body["model"]))
        async with self._client.stream(
            "POST", f"{self._base_url}/messages", json=body, headers=self._headers
        ) as response:
            if response.status_code != 200:
                text = (await response.aread()).decode(errors="replace")
                raise ProviderError(f"Anthropic HTTP {response.status_code}: {text}")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                progressed[0] = True
                delta_text = acc.feed(json.loads(line[len("data: ") :]))
                if delta_text:
                    yield TextDelta(delta_text)
        yield acc.completion()


class _Accumulator:
    """Assembles Messages API stream events into text, tool calls, and usage."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._text: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._input_tokens = 0
        self._output_tokens = 0

    def feed(self, event: dict[str, Any]) -> str:
        """Consume one stream event; return any new assistant text."""
        kind = event.get("type")
        if kind == "error":
            error = event.get("error") or {}
            raise ProviderError(
                f"Anthropic stream error {error.get('type')}: {error.get('message')}"
            )
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            self._input_tokens = int(usage.get("input_tokens", 0))
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            self._output_tokens = int(usage.get("output_tokens", 0))
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._calls[int(event["index"])] = {
                    "id": str(block["id"]), "name": str(block["name"]), "json": ""
                }
        elif kind == "content_block_delta":
            return self._delta(int(event["index"]), event.get("delta") or {})
        return ""

    def _delta(self, index: int, delta: dict[str, Any]) -> str:
        if delta.get("type") == "text_delta":
            text = str(delta.get("text", ""))
            self._text.append(text)
            return text
        if delta.get("type") == "input_json_delta" and index in self._calls:
            self._calls[index]["json"] += str(delta.get("partial_json", ""))
        return ""

    def completion(self) -> Completion:
        calls = tuple(
            ToolCall(
                id=slot["id"],
                name=slot["name"],
                arguments=parse_arguments(slot["json"]),
            )
            for _, slot in sorted(self._calls.items())
        )
        usage = Usage(
            prompt_tokens=self._input_tokens,
            completion_tokens=self._output_tokens,
            cost=cost_usd(self._model, self._input_tokens, self._output_tokens),
        )
        return Completion(text="".join(self._text), tool_calls=calls, usage=usage)

