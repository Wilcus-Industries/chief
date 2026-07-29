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


def scope_values(scope: Any) -> dict[str, str]:
    """The usable scope entries — non-empty ``sender``/``thread_key`` only.

    One source of truth for "does this row have a scope", so the boot sweep and
    the runtime check can't disagree about an empty or malformed one.
    """
    if not isinstance(scope, dict):
        return {}
    return {f: str(scope[f]) for f in ("sender", "thread_key") if scope.get(f)}


def is_unscoped_classifier(predicate: dict[str, Any]) -> bool:
    """A classifier predicate with no usable scope — the leak #285 closes."""
    kind_is_classifier = predicate.get("kind") == "classifier"
    return kind_is_classifier and not scope_values(predicate.get("scope"))


def in_scope(predicate: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether this event is one the monitor is allowed to read (#285).

    A classifier predicate without a usable scope matches nothing — fail
    closed, so a row written before this rule (or by any other path) can't leak
    while it waits to be disabled at load. Two scope fields at once is likewise
    dead: creation forbids it, so such a row was hand-written and honouring
    either half would silently drop the other constraint.
    """
    wanted = scope_values(predicate.get("scope"))
    if len(wanted) != 1:
        # No scope at all is fine for a local regex, and only for that.
        return not wanted and predicate.get("kind") == "code"
    field, value = next(iter(wanted.items()))
    return str(payload.get(field, "")).lower() == value.lower()


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
    # Whitespace-only is no scope at all: it would build a monitor that reports
    # success and can never match, which is a dead security control.
    scope_sender = (scope_sender or "").strip() or None
    scope_thread = (scope_thread or "").strip() or None
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
            "email) or scope_thread (one thread_key, e.g. a group chat). The "
            "value must equal the event field exactly (+16505551212, not "
            "650-555-1212). Ask the owner which contact or group; don't guess."
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
