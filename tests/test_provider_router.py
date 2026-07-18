"""RouterProvider: model-name routing across named backends with rewrite."""

from collections.abc import AsyncIterator
from typing import Any

from chief.provider.base import Completion, ProviderEvent, TextDelta, ToolSpec
from chief.provider.router import RouterProvider


class RecordingProvider:
    """A backend that records the model id it was called with."""

    def __init__(self, reply: str = "ok") -> None:
        self.models: list[str] = []
        self._reply = reply

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
    ) -> AsyncIterator[ProviderEvent]:
        self.models.append(model)
        yield TextDelta(self._reply)
        yield Completion(text=self._reply)


async def drain(provider: RouterProvider, model: str) -> list[ProviderEvent]:
    events: list[ProviderEvent] = []
    async for event in provider.stream(model=model, messages=[], tools=[]):
        events.append(event)
    return events


async def test_alias_routes_to_backend_and_rewrites_model() -> None:
    proxy = RecordingProvider("from-proxy")
    default = RecordingProvider("from-default")
    router = RouterProvider(
        default=default,
        backends={"proxy": proxy},
        aliases={"opus": ("proxy", "claude-opus-4-8")},
    )
    events = await drain(router, "opus")
    # The typed name "opus" is rewritten to the backend's real model id.
    assert proxy.models == ["claude-opus-4-8"]
    assert default.models == []
    assert events[-1] == Completion(text="from-proxy")


async def test_unlisted_model_delegates_to_default_unchanged() -> None:
    proxy = RecordingProvider()
    default = RecordingProvider("from-default")
    router = RouterProvider(
        default=default,
        backends={"proxy": proxy},
        aliases={"opus": ("proxy", "claude-opus-4-8")},
    )
    events = await drain(router, "qwen/qwen3-coder")
    assert default.models == ["qwen/qwen3-coder"]
    assert proxy.models == []
    assert isinstance(events[-1], Completion)


async def test_streamed_events_pass_through_unbuffered() -> None:
    default = RecordingProvider("hi")
    router = RouterProvider(default=default, backends={}, aliases={})
    events = await drain(router, "m")
    assert events == [TextDelta("hi"), Completion(text="hi")]


async def test_alias_can_target_the_default_backend_by_name() -> None:
    default = RecordingProvider("d")
    router = RouterProvider(
        default=default,
        backends={"default": default},
        aliases={"fast": ("default", "qwen/qwen3-coder")},
    )
    await drain(router, "fast")
    assert default.models == ["qwen/qwen3-coder"]
