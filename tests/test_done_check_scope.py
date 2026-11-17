"""The done-check's mypy scope leaves untracked instance data alone.

The self-edit seatbelt runs ``uv run mypy .`` on a box. ``skills/`` there is
untracked instance data seeded from ``core-skills/`` plus whatever packages
installed (their scripts are type-checked in their own repo); if mypy walks
it, one package's script can turn every self-edit restart red.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_mypy_excludes_untracked_instance_dirs() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    exclude = config["tool"]["mypy"]["exclude"]
    for instance_dir in ("data", "skills"):
        assert f"/{instance_dir}/" in (ROOT / ".gitignore").read_text()
        assert f"^{instance_dir}/" in exclude
