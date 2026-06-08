"""Integration test: Alembic baseline migration matches Base.metadata with no drift.

Acceptance criteria from issue #2:
- ``alembic upgrade head`` against an empty temp sqlite produces a schema equal
  to the current models.
- ``alembic``'s autogenerate detects **no drift** afterward.

Acceptance criteria from issue #10:
- ``alembic upgrade head`` does not disable pre-configured ``chief.*`` loggers.

Acceptance criteria from issue #11:
- ``_run_migrations`` works from an arbitrary CWD (regression guard).

Acceptance criteria from issue #13:
- ``_ALEMBIC_INI`` resolves to an existing file from the package-install location,
  not just from CWD=repo-root (guards against Dockerfile.core missing the alembic
  config/versions in the built image).
- The ``alembic/`` script directory resolved by ``_run_migrations`` also exists.

Acceptance criteria from issue #14:
- Drift tests (upgrade-head + autogenerate) run against the SAME runtime copy that
  ``_run_migrations`` uses (``src/chief/alembic/``), not a separate dev-only copy.
  This closes the repro where a column dropped from the runtime migration only passes
  CI green because the drift tests were pointing at a different tree.
"""

import importlib.resources
import logging
import os
import tempfile
from collections.abc import Generator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from chief.persistence.db import _ALEMBIC_INI as _PKG_ALEMBIC_INI
from chief.persistence.db import _run_migrations
from chief.persistence.models import Base


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
    """Return an Alembic Config pointing at the RUNTIME (package) copy.

    Uses the same ``_PKG_ALEMBIC_INI`` and script directory that
    ``_run_migrations`` uses, so these tests guard the copy that ships in the
    Docker image — not a separate dev-only tree that could silently diverge.
    """
    cfg = Config(str(_PKG_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    # Pin to the package script dir (absolute) so tests are CWD-independent.
    cfg.set_main_option("script_location", str(_PKG_ALEMBIC_INI.parent / "alembic"))
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


def test_migration_does_not_disable_existing_loggers(temp_db_url: str) -> None:
    """Running ``alembic upgrade head`` must not disable pre-configured loggers.

    Regression for issue #10: ``fileConfig`` in env.py previously defaulted
    ``disable_existing_loggers=True``, which silenced every ``chief.*`` logger
    that had been configured before the migration ran.
    """
    # Configure a chief.* logger before running any migration.
    logger = logging.getLogger("chief.regression_test_issue_10")
    logger.disabled = False

    cfg = _alembic_cfg(temp_db_url)
    command.upgrade(cfg, "head")

    assert not logger.disabled, (
        "alembic upgrade head disabled the chief.regression_test_issue_10 logger; "
        "env.py must pass disable_existing_loggers=False to fileConfig"
    )


def test_run_migrations_succeeds_from_arbitrary_cwd() -> None:
    """_run_migrations must work regardless of the process's CWD.

    Regression for issue #11: alembic.ini had ``script_location = alembic``
    (relative), so running from a directory without an ``alembic/`` sub-dir
    raised CommandError.  The fix pins script_location to an absolute path.
    """
    tmp_cwd = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_file:
        db_path = db_file.name

    orig_cwd = os.getcwd()
    try:
        os.chdir(tmp_cwd)
        # Must not raise CommandError or any other exception.
        _run_migrations(db_path)
    finally:
        os.chdir(orig_cwd)
        os.rmdir(tmp_cwd)

    try:
        # Verify the migration actually created the schema.
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            tables = {row[0] for row in result}
        assert set(Base.metadata.tables.keys()) <= tables
    finally:
        os.unlink(db_path)


def test_alembic_ini_resolves_from_package_location() -> None:
    """_ALEMBIC_INI must exist regardless of the Python install layout.

    Regression guard for issue #13: the old path used ``Path(__file__).parents[3]``
    which resolves correctly in a repo checkout (editable install) but lands in
    a wrong directory (e.g. site-packages ancestor) when installed as a wheel.
    The fix bundles alembic.ini inside the chief package so importlib.resources
    always finds it.
    """
    assert _PKG_ALEMBIC_INI.exists(), (
        f"_ALEMBIC_INI={_PKG_ALEMBIC_INI!r} does not exist. "
        "alembic.ini must be bundled as package data so it is found in both "
        "editable (dev) and wheel (Docker) installs."
    )


def test_alembic_script_dir_resolves_from_package_location() -> None:
    """The alembic/ script directory must exist next to _ALEMBIC_INI.

    Regression guard for issue #13: the migrations directory must be co-located
    with alembic.ini so _run_migrations can locate the version scripts in the
    built Docker image (wheel install).
    """
    script_dir = _PKG_ALEMBIC_INI.parent / "alembic"
    assert script_dir.is_dir(), (
        f"alembic/ script dir={script_dir!r} does not exist. "
        "The alembic/ directory must be bundled as package data alongside "
        "alembic.ini so migrate-on-start works in the Docker image."
    )
    # At minimum the versions/ subdir and env.py must be present.
    assert (script_dir / "versions").is_dir(), (
        f"alembic/versions/ not found under {script_dir!r}"
    )
    assert (script_dir / "env.py").exists(), (
        f"alembic/env.py not found under {script_dir!r}"
    )


def test_alembic_resources_accessible_via_importlib() -> None:
    """importlib.resources can locate alembic.ini inside the chief package.

    Asserts the package data is properly declared in pyproject.toml so it
    survives into a wheel (and thus into the Docker image) without needing
    an explicit COPY step.
    """
    ref = importlib.resources.files("chief").joinpath("alembic.ini")
    # traversable.is_file() works for both real files and zip-embedded resources
    assert ref.is_file(), (
        "chief/alembic.ini is not accessible via importlib.resources. "
        "Add it to the hatchling include list in pyproject.toml."
    )
