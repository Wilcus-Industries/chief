"""distill(): JSON drafts parsed into facts; malformed JSON → no writes, logged."""

import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import AssistantMessage, TextBlock

from chief.memory.distill import distill
from chief.memory.store import FactDraft

_TRANSCRIPT = [("owner", "I prefer mornings for calls"), ("chief", "Noted.")]


def _query_returning(text: str) -> Any:
    async def _gen(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=text)], model="m")

    return _gen


async def test_distill_parses_json_array_into_drafts() -> None:
    payload = (
        '[{"slug": "mornings", "title": "Prefers mornings", '
        '"body": "Books calls before noon.", "trust": "high"}]'
    )
    drafts = await distill(
        _TRANSCRIPT, index="", model="m", query=_query_returning(payload)
    )

    assert drafts == [
        FactDraft(
            slug="mornings",
            title="Prefers mornings",
            body="Books calls before noon.",
            trust="high",
        )
    ]


async def test_distill_handles_expires_and_code_fence() -> None:
    payload = (
        "```json\n"
        '[{"slug": "vacation", "title": "On vacation", "body": "Back Monday.", '
        '"expires": "2026-06-10T00:00:00+00:00"}]\n'
        "```"
    )
    drafts = await distill(
        _TRANSCRIPT, index="", model="m", query=_query_returning(payload)
    )

    assert len(drafts) == 1
    assert drafts[0].slug == "vacation"
    assert drafts[0].expires == "2026-06-10T00:00:00+00:00"
    assert drafts[0].trust == "medium"  # default when the model omits it


async def test_distill_empty_array_writes_nothing() -> None:
    drafts = await distill(
        _TRANSCRIPT, index="", model="m", query=_query_returning("[]")
    )
    assert drafts == []


async def test_distill_malformed_json_logs_and_returns_empty(
    caplog: Any,
) -> None:
    with caplog.at_level(logging.WARNING, logger="chief.memory.distill"):
        drafts = await distill(
            _TRANSCRIPT, index="", model="m", query=_query_returning("not json at all")
        )

    assert drafts == []
    assert any("distill" in r.message.lower() for r in caplog.records)


async def test_distill_skips_entries_missing_required_fields() -> None:
    payload = '[{"slug": "ok", "title": "T", "body": "B"}, {"title": "no slug"}]'
    drafts = await distill(
        _TRANSCRIPT, index="", model="m", query=_query_returning(payload)
    )

    assert [d.slug for d in drafts] == ["ok"]


async def test_distill_query_failure_returns_empty(caplog: Any) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sdk down")

    with caplog.at_level(logging.WARNING, logger="chief.memory.distill"):
        drafts = await distill(_TRANSCRIPT, index="", model="m", query=_boom)

    assert drafts == []
