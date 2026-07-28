"""Build a monitor's match predicate from the tool's three creation forms.

Kept separate from the tool dispatch so ``tools.py`` stays about actions and
this stays about the pattern/instruction/classifier shapes and their validation.
"""

from collections.abc import Callable
from typing import Any

from chief.classifiers import ClassifierDef

# The `message.inbound` payload keys a `pattern` monitor can match on — see
# Dispatcher._publish_inbound. A pattern searches exactly ONE of these, so a
# field outside the set would match "" forever and never fire (#235).
MATCHABLE_FIELDS = ("text", "sender", "thread_key")


def is_unscoped_classifier(predicate: dict[str, Any]) -> bool:
    """A classifier predicate with no scope — the leak #285 closes."""
    return predicate.get("kind") == "classifier" and not isinstance(
        predicate.get("scope"), dict
    )


def in_scope(predicate: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether this event is one the monitor is allowed to read (#285).

    A classifier predicate without a scope matches nothing — fail closed, so a
    row written before this rule (or by any other path) can't leak while it
    waits to be disabled at load.
    """
    scope = predicate.get("scope")
    if not isinstance(scope, dict):
        return predicate.get("kind") == "code"
    for field in ("sender", "thread_key"):
        wanted = scope.get(field)
        if wanted:
            return str(payload.get(field, "")).lower() == str(wanted).lower()
    return False


def build_predicate(
    pattern: str | None,
    instruction: str | None,
    classifier: str | None,
    fire_label: str | None,
    field: str | None,
    classifier_def: Callable[[str], ClassifierDef | None],
    scope_sender: str | None = None,
    scope_thread: str | None = None,
) -> dict[str, Any] | str:
    """The predicate dict, or an ``error: ...`` string if the forms are invalid.

    Exactly one of pattern/instruction/classifier must be given; ``field`` only
    applies to the pattern form; a named classifier needs a declared fire_label.

    A classifier form must also be scoped to one sender or one thread (#285):
    matching it sends the event payload to a model, so an unscoped one feeds
    every stranger's words to the judge. A pattern is local regex over one
    field — nothing leaves the machine — so it needs no scope.
    """
    forms = [pattern, instruction, classifier]
    if sum(form is not None for form in forms) != 1:
        return "error: give exactly one of pattern, instruction, or classifier"
    if classifier is not None and not fire_label:
        return "error: classifier needs a fire_label"
    if field is not None and pattern is None:
        return "error: field only applies to the pattern form"
    if field is not None and field not in MATCHABLE_FIELDS:
        return (
            f"error: '{field}' is not a matchable event field "
            f"({', '.join(MATCHABLE_FIELDS)})"
        )
    if scope_sender and scope_thread:
        return "error: give one of scope_sender or scope_thread, not both"
    scope = (
        {"sender": scope_sender}
        if scope_sender
        else {"thread_key": scope_thread} if scope_thread else None
    )
    if pattern is not None:
        if scope is not None:
            return (
                "error: scope only applies to the instruction and classifier "
                "forms; a pattern matches a contact with field=sender"
            )
        return {"kind": "code", "field": field or "text", "pattern": pattern}
    if classifier is not None:
        definition = classifier_def(classifier)
        if definition is None:
            return f"error: unknown classifier '{classifier}'"
        if fire_label not in definition.labels:
            return (
                f"error: fire_label '{fire_label}' is not a declared label "
                f"of '{classifier}' ({', '.join(definition.labels)})"
            )
    if scope is None:
        return (
            "error: a classifier monitor must be scoped to whose messages it "
            "may read — give scope_sender (one contact's handle, number, or "
            "email) or scope_thread (one thread_key, e.g. a group chat). Ask "
            "the owner which contact or group; do not guess."
        )
    if instruction is not None:
        return {
            "kind": "classifier",
            "classifier": "wake-judge",
            "fire_label": "YES",
            "instruction": instruction,
            "scope": scope,
        }
    return {
        "kind": "classifier",
        "classifier": classifier,
        "fire_label": fire_label,
        "scope": scope,
    }
