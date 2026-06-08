"""Integration test: Alembic baseline migration matches Base.metadata with no drift.

Acceptance criteria from issue #2:
- ``alembic upgrade head`` against an empty temp sqlite produces a schema equal
  to the current models.
- ``alembic``'s autogenerate detects **no drift** afterward.
"""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from chief.persistence.models import Base

# Repo-root alembic.ini lives one level above tests/
_REPO_ROOT = Path(__file__).parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


@pytest.fixture
def temp_db_url() -> Generator[str, None, None]:
    """Yield a ``sqlite:///`` URL pointing at a fresh temp file, deleted after."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    url = f"sqlite:///{db_path}"
    try:
        yield url
    finally:
        os.unlink(db_path)


def _alembic_cfg(db_url: str) -> Config:
    """Return an Alembic Config with sqlalchemy.url overridden to ``db_url``."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_upgrade_head_creates_full_schema(temp_db_url: str) -> None:
    """upgrade head against an empty sqlite produces every table in Base.metadata."""
    cfg = _alembic_cfg(temp_db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(temp_db_url)
    with engine.connect() as conn:
        # Confirm every model table exists after upgrade.
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        actual_tables = {row[0] for row in result}

    expected_tables = set(Base.metadata.tables.keys())
    assert expected_tables <= actual_tables, (
        f"Missing after upgrade: {expected_tables - actual_tables}"
    )


def test_autogenerate_detects_no_drift(temp_db_url: str) -> None:
    """After upgrade head, autogenerate must report zero schema diff."""
    cfg = _alembic_cfg(temp_db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(temp_db_url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diffs = compare_metadata(context, Base.metadata)

    # compare_metadata returns a list of diff tuples; empty means no drift.
    assert diffs == [], f"Schema drift detected after upgrade head:\n{diffs}"
