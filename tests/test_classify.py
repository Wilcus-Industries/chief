"""Haiku stop-intent / warrants-a-task classifiers (SDK mocked)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from chief.core import classify


def _stream(text: str) -> Any:
    async def _gen(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=text)], model="claude-haiku-4-5")

    return _gen


@pytest.mark.parametrize(
    "answer,expected",
    [("YES", True), ("Yes, stop that", True), ("NO", False), ("", False)],
)
async def test_stop_intent_parses(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
) -> None:
    monkeypatch.setattr(classify, "query", _stream(answer))
    assert await classify.stop_intent("never mind", model="m") is expected


async def test_warrants_task_parses_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify, "query", _stream("YES"))
    assert await classify.warrants_task("book a flight Friday", model="m") is True


async def test_classifier_failure_defaults_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sdk down")

    monkeypatch.setattr(classify, "query", _boom)
    assert await classify.stop_intent("x", model="m") is False
    assert await classify.warrants_task("x", model="m") is False
