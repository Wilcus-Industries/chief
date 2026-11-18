"""Match one inbound event against its in-scope monitors, screening it once.

Regex (``code``) monitors match locally on the event payload. If any matched,
or any classifier monitor is in scope, the thread's chat log (the event in
context, see wake.py) goes to the :class:`~chief.screen.Screen` in a single
call: the injection question plus one ``judge_<id>`` Choice per
classifier monitor — one TypeSafe request per event, not one per monitor.
When TypeSafe did not answer the judges (no key, error), each falls back to its
own classifier model turn. No match and no classifier in scope: zero requests.
"""

import logging
import re
from typing import Any

from chief.classifiers import Classifier, _match_label
from chief.persistence.models import MonitorRow
from chief.screen import Screen, Verdict
from chief.typesafe import Answer, choice

logger = logging.getLogger(__name__)


class MonitorGate:
    """Which monitors fire on an event, and the screen's verdict on it."""

    def __init__(self, classifier: Classifier, screen: Screen | None) -> None:
        self._classifier = classifier
        self._screen = screen

    async def evaluate(
        self, monitors: list[MonitorRow], payload: dict[str, Any], text: str,
        *, untrusted: bool = True,
    ) -> tuple[list[MonitorRow], Verdict | None]:
        """``(fired monitors, verdict)`` for the event ``payload``, judged and
        screened as ``text`` (its chat log). ``untrusted=False`` — every line
        is the owner's or chief's own — skips the screen. The verdict is
        ``None`` then, without a screen, or when nothing could fire."""
        fired = [m for m in monitors if _code_match(m, payload)]
        judged = [m for m in monitors if m.predicate.get("kind") == "classifier"]
        if not fired and not judged:
            return [], None
        verdict: Verdict | None = None
        answers: dict[str, Answer] = {}
        if self._screen is not None and untrusted:
            extra = {
                f"judge_{m.id}": q for m in judged if (q := self._question(m))
            }
            verdict = await self._screen.check(
                text, spend_key="screen:monitor", extra=extra
            )
            answers = verdict.answers
        for monitor in judged:
            # One monitor's judge raising must not drop its siblings.
            try:
                label = await self._label(monitor, text, answers, verdict)
            except Exception:
                logger.exception("monitor %s errored", monitor.id)
                continue
            if label == str(monitor.predicate["fire_label"]):
                fired.append(monitor)
        return fired, verdict

    async def screen(self, text: str) -> Verdict | None:
        """Screen a wake's lines the judge did not see; ``None`` without a screen."""
        if self._screen is None:
            return None
        return await self._screen.check(text, spend_key="screen:monitor")

    def _question(self, monitor: MonitorRow) -> dict[str, Any] | None:
        definition = self._classifier.definition(str(monitor.predicate["classifier"]))
        if definition is None:
            return None
        instructions = definition.prompt
        if (instruction := monitor.predicate.get("instruction")) is not None:
            instructions += (
                f"\n\nThe instruction to judge: {instruction}\n"
                "The state is the thread's recent messages as a chat log, oldest "
                "first: the line marked ▶ is the new message to judge, `you` is "
                "the assistant, and (owner) marks the owner."
            )
        return choice(instructions, definition.labels)

    async def _label(
        self, monitor: MonitorRow, text: str, answers: dict[str, Answer],
        verdict: Verdict | None,
    ) -> str | None:
        name = str(monitor.predicate["classifier"])
        answer = answers.get(f"judge_{monitor.id}")
        definition = self._classifier.definition(name)
        if answer is not None and answer.choice and definition is not None:
            return _match_label(answer.choice, definition.labels)
        instruction = monitor.predicate.get("instruction")
        if instruction is not None:
            text = f"{instruction}\n\nConversation:\n{text}"
        # The screen already asked TypeSafe and it did not answer: skip it.
        return await self._classifier.classify(name, text, typesafe=verdict is None)


def _code_match(monitor: MonitorRow, payload: dict[str, Any]) -> bool:
    predicate = monitor.predicate
    if predicate.get("kind") != "code":
        return False
    value = str(payload.get(str(predicate.get("field", "text")), ""))
    # A bad pattern (unvalidated regex) must cost only its own monitor.
    try:
        return re.search(str(predicate["pattern"]), value, re.I) is not None
    except Exception:
        logger.exception("monitor %s has a bad pattern", monitor.id)
        return False
