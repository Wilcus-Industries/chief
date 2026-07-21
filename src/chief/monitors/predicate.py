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


def build_predicate(
    pattern: str | None,
    instruction: str | None,
    classifier: str | None,
    fire_label: str | None,
    field: str | None,
    classifier_def: Callable[[str], ClassifierDef | None],
) -> dict[str, Any] | str:
    """The predicate dict, or an ``error: ...`` string if the forms are invalid.

    Exactly one of pattern/instruction/classifier must be given; ``field`` only
    applies to the pattern form; a named classifier needs a declared fire_label.
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
    if pattern is not None:
        return {"kind": "code", "field": field or "text", "pattern": pattern}
    if instruction is not None:
        return {
            "kind": "classifier",
            "classifier": "wake-judge",
            "fire_label": "YES",
            "instruction": instruction,
        }
    assert classifier is not None  # the exactly-one check guarantees it
    definition = classifier_def(classifier)
    if definition is None:
        return f"error: unknown classifier '{classifier}'"
    if fire_label not in definition.labels:
        return (
            f"error: fire_label '{fire_label}' is not a declared label "
            f"of '{classifier}' ({', '.join(definition.labels)})"
        )
    return {"kind": "classifier", "classifier": classifier, "fire_label": fire_label}
