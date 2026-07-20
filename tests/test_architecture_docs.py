"""Self-knowledge docs: ARCHITECTURE.md exists and the self-edit skill body
points at it — the always-reachable discovery anchor for self-changes.

ARCHITECTURE.md is an index, so its value depends on the links resolving. A
dangling link sends a self-editing agent looking for a file that isn't there,
which is worse than no link at all — hence the link check below.
"""

import re
from pathlib import Path

from chief.skills import SkillLibrary

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# [text](target) where target is neither a URL nor a bare in-page anchor.
_LINK = re.compile(r"\[[^\]]*\]\((?!https?://|#)([^)]+)\)")


def test_architecture_doc_exists() -> None:
    assert (DOCS_DIR / "ARCHITECTURE.md").is_file()


def test_self_edit_skill_points_to_architecture_doc() -> None:
    skill = SkillLibrary(REPO_ROOT / "skills").get("self-edit")
    assert skill is not None
    assert "docs/ARCHITECTURE.md" in skill.body()


def test_architecture_index_links_every_sibling_doc() -> None:
    index = (DOCS_DIR / "ARCHITECTURE.md").read_text()
    siblings = {p.name for p in DOCS_DIR.glob("*.md")} - {"ARCHITECTURE.md"}
    missing = sorted(name for name in siblings if name not in index)
    assert not missing, f"docs not linked from ARCHITECTURE.md: {missing}"


def test_doc_relative_links_resolve() -> None:
    broken: list[str] = []
    for doc in sorted(DOCS_DIR.glob("*.md")):
        for target in _LINK.findall(doc.read_text()):
            path = target.split("#", 1)[0]
            if path and not (doc.parent / path).resolve().exists():
                broken.append(f"{doc.name} -> {target}")
    assert not broken, f"dangling doc links: {broken}"
