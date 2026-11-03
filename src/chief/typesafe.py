"""TypeSafe System One client: typed judgments (Noul / Choice) over raw httpx.

``POST {base_url}/systemone`` with a Bearer key. One request carries a ``state``
(the content to judge) and any number of named ``questions``; the reply holds
one typed answer per question. A Noul answers yes/no as a 0-1 score, a Choice
picks one declared option. The answer is a number or an enum, never free text,
so content under judgment cannot talk the screen into a chosen reply.

Raw httpx, no SDK (DESIGN.md: own harness, no vendor SDK). Every failure —
transport, timeout, non-200, malformed body — raises :class:`TypeSafeError`
so callers fall back rather than guess. Cost is input-token priced and recorded
on the shared budget under the caller's spend key.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"
DEFAULT_USD_PER_MTOK = 0.042


class SpendSink(Protocol):
    """Where request cost lands — :class:`chief.budget.Budget` in production."""

    async def record(self, thread_key: str, cost: float) -> None: ...


class TypeSafeError(RuntimeError):
    """TypeSafe could not answer (transport, timeout, HTTP error, bad body)."""


@dataclass(frozen=True)
class Answer:
    """One typed answer. ``noul`` is set for Noul questions, ``choice`` for
    Choice questions; ``confidence`` when the API reports one."""

    type: str
    noul: float | None = None
    choice: str | None = None
    confidence: float | None = None


def noul(instructions: str) -> dict[str, Any]:
    """A yes/no question answered as a 0-1 score."""
    return {"type": "noul", "instructions": instructions}


def choice(instructions: str, labels: tuple[str, ...]) -> dict[str, Any]:
    """A pick-one question; each label is its own option."""
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {label: label for label in labels},
    }


class Asker(Protocol):
    """Anything that answers typed questions — :class:`TypeSafeClient`, or a
    test double."""

    async def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
        *,
        spend_key: str,
    ) -> dict[str, "Answer"]: ...


class TypeSafeClient:
    """Asks TypeSafe typed questions about one piece of state."""

    def __init__(
        self,
        api_key: str,
        *,
        budget: SpendSink | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        usd_per_mtok: float = DEFAULT_USD_PER_MTOK,
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._budget = budget
        self._url = base_url.rstrip("/") + "/systemone"
        self._model = model
        self._usd_per_mtok = usd_per_mtok
        self._timeout = timeout_s
        self._client = client or httpx.AsyncClient()

    async def ask(
        self,
        state: dict[str, Any],
        questions: dict[str, dict[str, Any]],
        *,
        spend_key: str,
    ) -> dict[str, Answer]:
        body = {"model": self._model, "state": state, "questions": questions}
        try:
            response = await self._client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise TypeSafeError(f"TypeSafe transport error: {exc!r}") from exc
        if response.status_code != 200:
            raise TypeSafeError(
                f"TypeSafe HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
            answers = {
                name: _answer(payload["answers"][name]) for name in questions
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TypeSafeError(f"TypeSafe malformed reply: {exc!r}") from exc
        # Bookkeeping must never discard a valid verdict.
        try:
            tokens = int((payload.get("usage") or {}).get("input_tokens") or 0)
            if self._budget is not None:
                await self._budget.record(
                    spend_key, tokens * self._usd_per_mtok / 1e6
                )
        except Exception:
            logger.warning("TypeSafe: could not record spend", exc_info=True)
        return answers


def _answer(raw: dict[str, Any]) -> Answer:
    kind = str(raw["type"])
    score = raw.get("noul")
    picked = raw.get("choice")
    confidence = raw.get("confidence")
    if kind == "noul" and not (
        isinstance(score, int | float) and 0.0 <= float(score) <= 1.0
    ):
        # Also rejects NaN (every comparison is False): a missing or junk
        # score must never read as 0.0 — clean.
        raise ValueError(f"noul answer without a 0-1 score: {score!r}")
    if kind == "choice" and picked is None:
        raise KeyError("choice answer without a pick")
    return Answer(
        type=kind,
        noul=None if score is None else float(score),
        choice=None if picked is None else str(picked),
        confidence=None if confidence is None else float(confidence),
    )
