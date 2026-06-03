"""One-shot owner agent turn, with the SDK query mocked."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from chief.core import agent


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def _patch_query(
    monkeypatch: pytest.MonkeyPatch, messages: list[Any]
) -> None:
    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        for message in messages:
            yield message

    monkeypatch.setattr(agent, "query", fake_query)


async def test_concatenates_assistant_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_query(monkeypatch, [_assistant("pong"), _assistant(" again")])

    reply = await agent.owner_oneshot("ping", model="claude-sonnet-4-6")

    assert reply == "pong again"


async def test_empty_stream_returns_no_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_query(monkeypatch, [])

    reply = await agent.owner_oneshot("ping", model="claude-sonnet-4-6")

    assert reply == agent.NO_REPLY
