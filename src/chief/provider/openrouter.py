"""OpenRouter implementation of the provider seam (SSE streaming)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from chief.provider.base import (
    Completion,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """Streams chat completions from OpenRouter's OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=30)
        )
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        """Yield TextDeltas as tokens arrive; end with one Completion."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            # Ask OpenRouter to report dollar cost in the final usage chunk.
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        acc = _StreamAccumulator()
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers,
            ) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    raise ProviderError(
                        f"OpenRouter HTTP {response.status_code}: {body}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data == "[DONE]":
                        break
                    delta_text = acc.feed(json.loads(data))
                    if delta_text:
                        yield TextDelta(delta_text)
        except httpx.TransportError as exc:
            # A down/unreachable backend (connect refused, transport, timeout)
            # must fail loud with context — never a silent model swap upstream.
            raise ProviderError(
                f"backend unreachable ({self._base_url}, model {model}): {exc}"
            ) from exc
        yield acc.completion()


class _StreamAccumulator:
    """Assembles SSE chunks into text, tool calls, and usage."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._usage = Usage()

    def feed(self, chunk: dict[str, Any]) -> str:
        """Consume one parsed SSE chunk; return any new assistant text."""
        if usage := chunk.get("usage"):
            self._usage = Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cost=usage.get("cost", 0.0),
            )
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            slot = self._calls.setdefault(
                tc["index"], {"id": "", "name": "", "arguments": ""}
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            function = tc.get("function") or {}
            if function.get("name"):
                slot["name"] += function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]
        content = delta.get("content")
        if content:
            self._text.append(content)
            return str(content)
        return ""

    def completion(self) -> Completion:
        calls = tuple(
            ToolCall(id=slot["id"], name=slot["name"], arguments=_parse_args(slot))
            for _, slot in sorted(self._calls.items())
        )
        return Completion(text="".join(self._text), tool_calls=calls, usage=self._usage)


def _parse_args(slot: dict[str, str]) -> dict[str, Any]:
    """Parse streamed argument JSON; surface malformed output to the tool layer."""
    raw = slot["arguments"] or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_unparsed_arguments": raw}
    if not isinstance(parsed, dict):
        return {"_unparsed_arguments": raw}
    return parsed
