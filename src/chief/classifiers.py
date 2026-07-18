"""Classifiers: an internal categorical-label primitive over the model seam.

A definition is a markdown file in the classifiers dir (YAML frontmatter: name,
description, labels, optional model; body: the classification prompt).
``classify`` runs a small model turn and returns exactly one declared label,
tolerant of case and surrounding whitespace, retrying twice more before raising
``ClassifierError``. It is internal + self-edit only — there is no agent-facing
tool. Monitors and other core services call it directly.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from chief.provider.base import Completion, Provider

logger = logging.getLogger(__name__)


class _LabelSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that leaves YAML 1.1 booleans (YES/NO/ON/OFF) as strings.

    Labels are written unquoted (``labels: [YES, NO]``); the stock resolver
    would coerce YES/NO into Python bools, so we drop the bool resolver down to
    just true/false and keep the label text intact.
    """


_LabelSafeLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in _LabelSafeLoader.yaml_implicit_resolvers.items()
}
_LabelSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


@dataclass(frozen=True)
class ClassifierDef:
    name: str
    description: str
    labels: tuple[str, ...]
    prompt: str
    model: str | None


class ClassifierError(RuntimeError):
    """A classifier could not resolve a valid label (unknown name, no match)."""


class ClassifierRegistry:
    """Scans the classifiers dir for definitions."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def scan(self) -> list[ClassifierDef]:
        return [d for f in sorted(self._root.glob("*.md")) if (d := _parse(f))]

    def get(self, name: str) -> ClassifierDef | None:
        return next((d for d in self.scan() if d.name == name), None)


def _parse(path: Path) -> ClassifierDef | None:
    text = path.read_text()
    if not text.startswith("---"):
        return None
    _, frontmatter, body = text.split("---", 2)
    meta = yaml.load(frontmatter, Loader=_LabelSafeLoader) or {}
    return ClassifierDef(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or "").strip(),
        labels=tuple(str(label) for label in (meta.get("labels") or ())),
        prompt=body.strip(),
        model=meta.get("model"),
    )


class Classifier:
    """Runs a named classifier definition against text, returning one label."""

    def __init__(
        self, provider: Provider, registry: ClassifierRegistry, default_model: str
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._default_model = default_model

    async def classify(self, name: str, text: str) -> str:
        definition = self._registry.get(name)
        if definition is None:
            raise ClassifierError(f"no classifier named '{name}'")
        model = definition.model or self._default_model
        system = (
            f"{definition.prompt}\n\nRespond with exactly one of these labels "
            "and nothing else:\n" + "\n".join(definition.labels)
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        for _ in range(3):
            reply = ""
            async for item in self._provider.stream(
                model=model, messages=messages, tools=[]
            ):
                if isinstance(item, Completion):
                    reply = item.text
            label = _match_label(reply, definition.labels)
            if label is not None:
                return label
        raise ClassifierError(f"classifier '{name}' returned no valid label")


def _match_label(reply: str, labels: tuple[str, ...]) -> str | None:
    norm = reply.strip().upper()
    for label in labels:
        if norm == label.upper():
            return label
    for label in labels:
        if norm.startswith(label.upper()):
            return label
    return None


def validate(root: Path) -> list[str]:
    """Return the well-formedness problems of every classifier def under root.

    Empty means each ``*.md`` has YAML frontmatter with a non-empty name,
    description, and labels list, plus a non-empty body. The self-edit
    done-check calls this, so a bogus write is rolled back rather than merged.
    """
    problems: list[str] = []
    for path in sorted(root.glob("*.md")):
        problems.extend(_validate_classifier(path))
    return problems


def _validate_classifier(path: Path) -> list[str]:
    text = path.read_text()
    if not text.startswith("---"):
        return [f"{path}: missing YAML frontmatter"]
    parts = text.split("---", 2)
    if len(parts) < 3:
        return [f"{path}: frontmatter has no closing '---'"]
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        return [f"{path}: frontmatter is not valid yaml ({exc})"]
    if not isinstance(meta, dict):
        return [f"{path}: frontmatter is not a mapping"]
    problems = []
    if not str(meta.get("name") or "").strip():
        problems.append(f"{path}: frontmatter missing 'name'")
    if not str(meta.get("description") or "").strip():
        problems.append(f"{path}: frontmatter missing 'description'")
    if not (meta.get("labels") or ()):
        problems.append(f"{path}: frontmatter missing 'labels'")
    if not parts[2].strip():
        problems.append(f"{path}: empty body")
    return problems
