"""Cheap classifiers over OpenRouter chat-completions (httpx), #88.

The text one-shots (stop-intent / warrants-task / complexity / category) hit OpenRouter
directly over httpx on a fixed cheap model — never a Copilot session, which would burn
the premium-request cap per message. ``httpx.MockTransport`` stands in for the network
here so the parsing, the fail-safe defaults, and the no-call-when-keyless guard are all
exercised for real. ``ask_condition`` (the tool-using monitor path) runs an ephemeral
Copilot session and is covered separately.
"""

import json
from typing import Any

import httpx

from chief.core import classify
from chief.core.session import Final

_KEY = "sk-or-test"


def _transport(reply: str, *, calls: list[httpx.Request] | None = None) -> Any:
    """A MockTransport returning ``reply`` as the assistant message content."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": reply}}]}
        )

    return httpx.MockTransport(handler)


def _boom_transport(calls: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "upstream down"})

    return httpx.MockTransport(handler)


async def test_stop_intent_parses_yes() -> None:
    assert (
        await classify.stop_intent(
            "no wait stop", model="m", api_key=_KEY, transport=_transport("YES")
        )
        is True
    )


async def test_stop_intent_parses_no() -> None:
    assert (
        await classify.stop_intent(
            "also add milk", model="m", api_key=_KEY, transport=_transport("NO")
        )
        is False
    )


async def test_warrants_task_parses_yes() -> None:
    assert (
        await classify.warrants_task(
            "book a flight Friday", model="m", api_key=_KEY, transport=_transport("YES")
        )
        is True
    )


async def test_is_complex_parses_yes() -> None:
    assert (
        await classify.is_complex(
            "design a sharded cache",
            model="m",
            api_key=_KEY,
            transport=_transport("YES"),
        )
        is True
    )


async def test_classifier_posts_to_openrouter_chat_completions() -> None:
    # The one-shot hits OpenRouter's chat-completions endpoint on the given model with
    # the key as a bearer token and the system + user messages in the body.
    calls: list[httpx.Request] = []
    await classify.is_complex(
        "x",
        model="the-cheap-model",
        api_key=_KEY,
        transport=_transport("NO", calls=calls),
    )
    assert len(calls) == 1
    req = calls[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["Authorization"] == f"Bearer {_KEY}"
    body = json.loads(req.content)
    assert body["model"] == "the-cheap-model"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]


async def test_classifier_makes_no_http_call_when_keyless() -> None:
    # AC: keyless → no HTTP call at all, and fail-safe to NO. The transport must never
    # be hit (calls stays empty) even though one is supplied.
    calls: list[httpx.Request] = []
    result = await classify.stop_intent(
        "stop", model="m", api_key=None, transport=_transport("YES", calls=calls)
    )
    assert result is False
    assert calls == []


async def test_classifier_failure_defaults_safe() -> None:
    # A non-2xx (or any error) fails safe to NO for each yes/no classifier.
    calls: list[httpx.Request] = []
    assert (
        await classify.stop_intent(
            "x", model="m", api_key=_KEY, transport=_boom_transport(calls)
        )
        is False
    )
    assert (
        await classify.warrants_task(
            "x", model="m", api_key=_KEY, transport=_boom_transport(calls)
        )
        is False
    )
    assert (
        await classify.is_complex(
            "x", model="m", api_key=_KEY, transport=_boom_transport(calls)
        )
        is False
    )


# ---- category classifier (#79) ----------------------------------------------

_CATEGORIES = ("writing", "code", "reasoning", "research", "general")


async def test_classify_category_returns_named_category() -> None:
    got = await classify.classify_category(
        "fix this stack trace",
        model="m",
        categories=_CATEGORIES,
        default="general",
        api_key=_KEY,
        transport=_transport("code"),
    )
    assert got == "code"


async def test_classify_category_unknown_reply_falls_back_to_default() -> None:
    got = await classify.classify_category(
        "??",
        model="m",
        categories=_CATEGORIES,
        default="general",
        api_key=_KEY,
        transport=_transport("banana"),
    )
    assert got == "general"


async def test_classify_category_failure_defaults_safe() -> None:
    calls: list[httpx.Request] = []
    got = await classify.classify_category(
        "x",
        model="m",
        categories=_CATEGORIES,
        default="general",
        api_key=_KEY,
        transport=_boom_transport(calls),
    )
    assert got == "general"


async def test_classify_category_no_call_when_keyless() -> None:
    calls: list[httpx.Request] = []
    got = await classify.classify_category(
        "summarize the literature",
        model="m",
        categories=_CATEGORIES,
        default="general",
        api_key=None,
        transport=_transport("research", calls=calls),
    )
    assert got == "general"  # fail-safe to default
    assert calls == []


async def test_classify_category_folds_descriptions_into_the_prompt() -> None:
    # #83: a re-described category steers the classifier — its blurb rides the system
    # prompt, so a self-config edit takes effect at the very next spawn.
    calls: list[httpx.Request] = []
    got = await classify.classify_category(
        "fix a null deref",
        model="m",
        categories=_CATEGORIES,
        default="general",
        descriptions={"code": "writing or fixing source code"},
        api_key=_KEY,
        transport=_transport("code", calls=calls),
    )
    assert got == "code"
    system = json.loads(calls[0].content)["messages"][0]["content"]
    assert "code (writing or fixing source code)" in system


# ---- ask_condition: the tool-using monitor path (ephemeral Copilot session) --------


class _FakeCondSession:
    """Records the session kwargs and replays a scripted final reply."""

    def __init__(
        self, reply: str, *, raise_on_turn: bool = False, **kwargs: Any
    ) -> None:
        self.kwargs = kwargs
        self._reply = reply
        self._raise = raise_on_turn
        self.closed = False
        self.session_id: str | None = None
        self.last_cost_usd = 0.0
        self.last_rate_limit_status: str | None = None
        self.last_served_model: str | None = None
        self.last_premium_requests: dict[str, int] = {}

    async def run_turn(self, text: str, attachments: Any = ()) -> Any:
        if self._raise:
            raise RuntimeError("copilot down")
        yield Final(text=self._reply)

    async def interrupt(self) -> None: ...
    async def set_model(self, model: str) -> None: ...
    async def aclose(self) -> None:
        self.closed = True


def _cond_factory(
    reply: str, *, raise_on_turn: bool = False, captured: dict[str, Any] | None = None
) -> Any:
    def factory(**kwargs: Any) -> _FakeCondSession:
        session = _FakeCondSession(reply, raise_on_turn=raise_on_turn, **kwargs)
        if captured is not None:
            captured["session"] = session
        return session

    return factory


async def test_ask_condition_allow_lists_exactly_its_read_tools() -> None:
    # AC: the ephemeral session is built with the read tools allow-listed, and its
    # can_use_tool ALLOWs exactly those and DENYs anything else (read-only monitor).
    captured: dict[str, Any] = {}
    reads = ["WebSearch", "mcp__gmail_chief__gmail_get_message"]
    result = await classify.ask_condition(
        "is there new mail?",
        model="m",
        allowed_tools=reads,
        session_factory=_cond_factory("YES", captured=captured),
    )
    assert result is True
    session = captured["session"]
    assert session.kwargs["allowed_tools"] == reads
    assert session.kwargs["system_prompt"]  # the condition system prompt is set
    can_use = session.kwargs["can_use_tool"]
    from chief.gate.types import PermissionResultAllow, PermissionResultDeny

    allowed = await can_use("WebSearch", {}, None)
    denied = await can_use("Write", {"file_path": "/etc/x"}, None)
    assert isinstance(allowed, PermissionResultAllow)
    assert isinstance(denied, PermissionResultDeny)
    assert session.closed is True  # the ephemeral session is torn down


async def test_ask_condition_fails_safe_to_no_on_error() -> None:
    session = classify.ask_condition(
        "?", model="m", allowed_tools=["WebSearch"],
        session_factory=_cond_factory("YES", raise_on_turn=True),
    )
    assert await session is False


async def test_ask_condition_parses_no() -> None:
    result = await classify.ask_condition(
        "?", model="m", allowed_tools=["WebSearch"],
        session_factory=_cond_factory("NO"),
    )
    assert result is False
