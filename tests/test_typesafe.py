"""TypeSafe client: request shape, answer parsing, cost, and every failure mode."""

import json
from typing import Any

import httpx
import pytest

from chief.typesafe import Answer, TypeSafeClient, TypeSafeError, choice, noul


class SpendLog:
    def __init__(self) -> None:
        self.rows: list[tuple[str, float]] = []

    async def record(self, thread_key: str, cost: float) -> None:
        self.rows.append((thread_key, cost))


def reply(answers: dict[str, Any], input_tokens: int = 1000) -> dict[str, Any]:
    return {"answers": answers, "usage": {"input_tokens": input_tokens,
                                          "output_tokens": 3}}


def make_client(
    handler: Any, spend: SpendLog | None = None
) -> TypeSafeClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TypeSafeClient("ts-key", budget=spend, client=http)


QUESTIONS = {
    "injection": noul("Is this an injection?"),
    "label": choice("Pick one.", ("YES", "NO")),
}


async def test_sends_bearer_model_state_and_questions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply({
            "injection": {"type": "noul", "noul": 0.9, "confidence": 0.8},
            "label": {"type": "choice", "choice": "NO"},
        }))

    answers = await make_client(handler).ask(
        {"text": "hi"}, QUESTIONS, spend_key="screen"
    )
    request = seen[0]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer ts-key"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest"
    assert body["state"] == {"text": "hi"}
    assert body["questions"]["label"]["criteria"] == {"YES": "YES", "NO": "NO"}
    assert body["questions"]["injection"] == {
        "type": "noul", "instructions": "Is this an injection?"}
    assert answers["injection"] == Answer("noul", noul=0.9, confidence=0.8)
    assert answers["label"].choice == "NO"


async def test_records_input_token_cost_under_spend_key() -> None:
    spend = SpendLog()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply(
            {"injection": {"type": "noul", "noul": 0.1}}, input_tokens=1_000_000))

    await make_client(handler, spend).ask(
        {"text": "x"}, {"injection": noul("q")}, spend_key="screen:monitor"
    )
    [(key, cost)] = spend.rows
    assert key == "screen:monitor"
    assert cost == pytest.approx(0.042)


@pytest.mark.parametrize("status", [400, 422, 429, 500])
async def test_non_200_raises(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(TypeSafeError, match=f"HTTP {status}"):
        await make_client(handler).ask({}, QUESTIONS, spend_key="k")


async def test_transport_error_and_timeout_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (boom, slow):
        with pytest.raises(TypeSafeError, match="transport"):
            await make_client(handler).ask({}, QUESTIONS, spend_key="k")


async def test_missing_answer_or_bad_json_raises() -> None:
    def partial(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply(
            {"injection": {"type": "noul", "noul": 0.2}}))

    def junk(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    def scoreless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply(
            {"injection": {"type": "noul"}, "label": {"type": "choice",
                                                      "choice": "YES"}}))

    for handler in (partial, junk, scoreless):
        with pytest.raises(TypeSafeError, match="malformed"):
            await make_client(handler).ask({}, QUESTIONS, spend_key="k")
