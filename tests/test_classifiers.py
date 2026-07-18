"""Classifiers: an internal categorical-label primitive over the provider seam.

Definitions are markdown files (frontmatter: name, description, labels,
optional model; body: the classification prompt). ``classify`` drains a model
turn to one declared label, tolerant of case/whitespace, retrying twice more
before raising ``ClassifierError``.
"""

from pathlib import Path

import pytest

from chief.classifiers import (
    Classifier,
    ClassifierDef,
    ClassifierError,
    ClassifierRegistry,
    _match_label,
    validate,
)

from .fakes import FakeProvider, text_turn

WAKE_JUDGE = """---
name: wake-judge
description: Yes/no judgment for a monitor instruction.
labels: [YES, NO]
---
Decide whether the instruction is satisfied by the event.
"""

WITH_MODEL = """---
name: fancy
description: Uses its own model.
labels: [A, B]
model: from/frontmatter
---
Pick a letter.
"""


def make_classifiers(tmp_path: Path, **files: str) -> ClassifierRegistry:
    root = tmp_path / "classifiers"
    root.mkdir()
    for name, text in files.items():
        (root / f"{name}.md").write_text(text)
    return ClassifierRegistry(root)


def test_registry_parses_frontmatter_and_labels(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    definitions = registry.scan()
    assert [d.name for d in definitions] == ["wake-judge"]
    definition = registry.get("wake-judge")
    assert isinstance(definition, ClassifierDef)
    assert definition.labels == ("YES", "NO")
    assert definition.description == "Yes/no judgment for a monitor instruction."
    assert definition.prompt.startswith("Decide whether the instruction")
    assert definition.model is None
    assert registry.get("nope") is None


async def test_classify_returns_matched_label(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    provider = FakeProvider([text_turn("YES")])
    classifier = Classifier(provider, registry, "default/model")
    assert await classifier.classify("wake-judge", "anything") == "YES"


async def test_classify_is_case_and_whitespace_tolerant(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    lower = Classifier(FakeProvider([text_turn("  yes \n")]), registry, "m")
    assert await lower.classify("wake-judge", "x") == "YES"
    prefixed = Classifier(FakeProvider([text_turn("YES, definitely")]), registry, "m")
    assert await prefixed.classify("wake-judge", "x") == "YES"


async def test_classify_retries_then_returns(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    provider = FakeProvider(
        [text_turn("maybe"), text_turn("nah"), text_turn("NO")]
    )
    classifier = Classifier(provider, registry, "m")
    assert await classifier.classify("wake-judge", "x") == "NO"
    assert len(provider.models) == 3


async def test_classify_raises_after_three_bad_replies(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    provider = FakeProvider([text_turn("x")] * 3)
    classifier = Classifier(provider, registry, "m")
    with pytest.raises(ClassifierError):
        await classifier.classify("wake-judge", "x")
    assert len(provider.models) == 3


async def test_unknown_classifier_name_raises(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    classifier = Classifier(FakeProvider([]), registry, "m")
    with pytest.raises(ClassifierError):
        await classifier.classify("ghost", "x")


async def test_model_resolution_prefers_frontmatter(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, fancy=WITH_MODEL)
    provider = FakeProvider([text_turn("A")])
    classifier = Classifier(provider, registry, "default/model")
    assert await classifier.classify("fancy", "x") == "A"
    assert provider.models[0] == "from/frontmatter"


async def test_model_resolution_falls_back_to_default(tmp_path: Path) -> None:
    registry = make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    provider = FakeProvider([text_turn("NO")])
    classifier = Classifier(provider, registry, "default/model")
    assert await classifier.classify("wake-judge", "x") == "NO"
    assert provider.models[0] == "default/model"


def test_match_label_prefers_longest_prefix() -> None:
    labels = ("SPAM", "SPAM_URGENT")
    assert _match_label("SPAM_URGENT!", labels) == "SPAM_URGENT"
    assert _match_label("SPAM here", labels) == "SPAM"
    assert _match_label("SPAM", labels) == "SPAM"  # exact pass still first


def test_validate_flags_malformed_definitions(tmp_path: Path) -> None:
    root = tmp_path / "classifiers"
    root.mkdir()
    (root / "no-name.md").write_text(
        "---\ndescription: d\nlabels: [A]\n---\nbody\n"
    )
    (root / "no-desc.md").write_text("---\nname: x\nlabels: [A]\n---\nbody\n")
    (root / "no-body.md").write_text(
        "---\nname: x\ndescription: d\nlabels: [A]\n---\n\n"
    )
    (root / "no-labels.md").write_text(
        "---\nname: x\ndescription: d\n---\nbody\n"
    )
    problems = validate(root)
    assert any("name" in p for p in problems)
    assert any("description" in p for p in problems)
    assert any("body" in p for p in problems)
    assert any("labels" in p for p in problems)


def test_validate_passes_well_formed(tmp_path: Path) -> None:
    make_classifiers(tmp_path, **{"wake-judge": WAKE_JUDGE})
    assert validate(tmp_path / "classifiers") == []


def test_validate_flags_bad_label_shapes(tmp_path: Path) -> None:
    root = tmp_path / "classifiers"
    root.mkdir()
    (root / "scalar-bool.md").write_text(
        "---\nname: x\ndescription: d\nlabels: true\n---\nbody\n"
    )
    (root / "scalar-str.md").write_text(
        "---\nname: x\ndescription: d\nlabels: NOPE\n---\nbody\n"
    )
    (root / "empty-label.md").write_text(
        "---\nname: x\ndescription: d\nlabels: ['']\n---\nbody\n"
    )
    problems = validate(root)
    assert any("scalar-bool" in p and "labels" in p for p in problems)
    assert any("scalar-str" in p and "labels" in p for p in problems)
    assert any("empty-label" in p for p in problems)
