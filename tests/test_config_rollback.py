"""A config-only restart must leave an undo behind.

`config.yaml` is gitignored, so `git reset --hard` steps past it. And a
config-only restart commits nothing, so before this it wrote no rollback
marker either — meaning that restart had no boot-failure recovery for
*anything*. The undo it needs is the newest config-history snapshot, which by
construction is the last config that booted (#301).
"""

import json
from pathlib import Path

from chief.config.history import restore_newest, snapshot
from chief.selfedit.recovery import MARKER_NAME, rollback_if_marked
from tests.test_config_history import at, write_config


def marker(repo: Path, **fields: object) -> None:
    (repo / MARKER_NAME).write_text(json.dumps(fields))


def test_restore_newest_puts_the_last_booted_config_back(tmp_path: Path) -> None:
    config = write_config(tmp_path, "good\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    config.write_text("bad\n")

    restored = restore_newest(history, config, tmp_path / "failed.yaml")

    assert restored is not None
    assert config.read_text() == "good\n"


def test_restore_newest_keeps_the_config_it_replaced(tmp_path: Path) -> None:
    """The config being rolled back may hold a hand edit the owner still
    wants; the boot failed, so it is replaced, but never simply discarded."""
    config = write_config(tmp_path, "good\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    config.write_text("hand edited but broken\n")
    failed = tmp_path / "failed.yaml"

    restore_newest(history, config, failed)

    assert failed.read_text() == "hand edited but broken\n"


def test_restore_newest_is_a_noop_when_config_already_matches(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path, "same\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    failed = tmp_path / "failed.yaml"

    assert restore_newest(history, config, failed) is None
    assert not failed.exists()


def test_restore_newest_with_no_history_does_nothing(tmp_path: Path) -> None:
    config = write_config(tmp_path, "only\n")

    assert restore_newest(tmp_path / "nope", config, tmp_path / "f.yaml") is None
    assert config.read_text() == "only\n"


def test_config_only_marker_rolls_the_config_back(tmp_path: Path) -> None:
    """The whole point: nothing was committed, so there is no git undo — the
    recovery is the snapshot, and the caller must still be told to reboot."""
    config = write_config(tmp_path, "good\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    config.write_text("bad\n")
    marker(tmp_path, committed=False)

    assert rollback_if_marked(tmp_path, history) is True
    assert config.read_text() == "good\n"
    assert not (tmp_path / MARKER_NAME).exists()


def test_a_marker_with_nothing_to_undo_does_not_reboot(tmp_path: Path) -> None:
    """Returning True here would crash-loop: restarting into the identical
    tree and identical config reproduces the same failed boot forever."""
    config = write_config(tmp_path, "same\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    marker(tmp_path, committed=False)

    assert rollback_if_marked(tmp_path, history) is False
    assert not (tmp_path / MARKER_NAME).exists()


def test_absent_marker_is_still_false(tmp_path: Path) -> None:
    assert rollback_if_marked(tmp_path, tmp_path / "history") is False


def test_legacy_marker_without_the_new_fields_still_resets(tmp_path: Path) -> None:
    """A marker written by the previous version is on disk across exactly the
    upgrade this ships in — it must not KeyError on the boot-failure path."""
    calls: list[list[str]] = []
    marker(tmp_path, rollback_to="deadbeef", rationale="old")

    assert rollback_if_marked(tmp_path, None, calls.append) is True
    assert calls == [["git", "reset", "--hard", "deadbeef"]]
