"""Releases: the version a box runs, and which release is the newest.

A release is a ``vX.Y.Z`` tag on core's repository. Only that exact shape
counts — a pre-release or a stray ``v1.2`` tag is not something a box may
update itself onto, so it is not a release at all.

Versions order numerically, never lexically: ``v0.10.0`` is newer than
``v0.9.0``, and a string sort gets that backwards.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Bounded like every other git call chief makes — a stalled repo must degrade
#: to "no answer", never to a hung dispatcher.
GIT_TIMEOUT_SECONDS = 30.0

_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"\d+\.\d+\.\d+"', re.MULTILINE)


@dataclass(frozen=True, order=True)
class Version:
    """A released version. Ordered by ``(major, minor, patch)``."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"v{self.number}"

    @property
    def number(self) -> str:
        """The bare ``X.Y.Z`` as it appears in ``pyproject.toml``."""
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, part: str) -> "Version":
        """The next version for ``major`` | ``minor`` | ``patch``."""
        if part == "major":
            return Version(self.major + 1, 0, 0)
        if part == "minor":
            return Version(self.major, self.minor + 1, 0)
        if part == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"unknown release part: {part!r}")


@dataclass(frozen=True)
class Release:
    """A released version and the commit its tag points at."""

    version: Version
    commit: str

    @property
    def tag(self) -> str:
        return str(self.version)


def parse_version(tag: str) -> Version | None:
    """``v1.2.3`` -> a Version; anything else -> ``None``."""
    match = _TAG_PATTERN.match(tag.strip())
    if match is None:
        return None
    return Version(*(int(part) for part in match.groups()))


def all_releases(repo_dir: Path) -> list[Release]:
    """Every ``vX.Y.Z`` tag in the repo, oldest first."""
    listed = _git(repo_dir, "tag", "--list", "v*")
    if listed is None:
        return []
    found = []
    for line in listed.splitlines():
        version = parse_version(line)
        if version is None:
            continue
        # Dereference: an annotated tag's own object is not the commit.
        commit = _git(repo_dir, "rev-list", "-n", "1", line.strip())
        if commit:
            found.append(Release(version=version, commit=commit.strip()))
    return sorted(found, key=lambda release: release.version)


def newest_release(repo_dir: Path) -> Release | None:
    """The highest-versioned release tag, or ``None`` if there are none."""
    found = all_releases(repo_dir)
    return found[-1] if found else None


def read_project_version(pyproject: Path) -> Version:
    """The version declared in ``pyproject.toml``."""
    match = _PYPROJECT_VERSION.search(pyproject.read_text())
    if match is None:
        raise ValueError(f"no version = \"X.Y.Z\" line in {pyproject}")
    version = parse_version("v" + match.group(0).split('"')[1])
    if version is None:  # pragma: no cover — the pattern already pinned the shape
        raise ValueError(f"unparsable version in {pyproject}")
    return version


def write_project_version(pyproject: Path, version: Version) -> None:
    """Rewrite the declared version in place, leaving the rest untouched."""
    text = pyproject.read_text()
    replaced, count = _PYPROJECT_VERSION.subn(
        f'version = "{version.number}"', text, count=1
    )
    if count != 1:
        raise ValueError(f"no version = \"X.Y.Z\" line in {pyproject}")
    pyproject.write_text(replaced)


def _git(repo_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 else None
