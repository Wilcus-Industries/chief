"""Snapshots of every config.yaml that booted healthy.

The seatbelt is git, and git only restores what it tracks — ``config.yaml`` is
gitignored, so a bad config write has no undo. These tests pin the recovery
path that replaces it.
"""

from datetime import UTC, datetime
from pathlib import Path

from chief.config.history import KEEP, snapshot, snapshots

MOMENT = datetime(2026, 8, 18, 14, 22, 1, tzinfo=UTC)


def at(second: int) -> datetime:
    """A distinct timestamp per call, so filenames sort in write order."""
    return MOMENT.replace(second=second)


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_first_snapshot_records_the_live_config(tmp_path: Path) -> None:
    config = write_config(tmp_path, "models:\n  default: qwen\n")
    history = tmp_path / "history"

    written = snapshot(config, history, at(1))

    assert written is not None
    assert written.read_text() == "models:\n  default: qwen\n"
    assert written.name == "2026-08-18T14-22-01Z.yaml"


def test_unchanged_config_is_not_snapshotted_again(tmp_path: Path) -> None:
    """The directory is a record of every *change*, not every boot — chief
    restarts far more often than its config moves."""
    config = write_config(tmp_path, "models:\n  default: qwen\n")
    history = tmp_path / "history"

    first = snapshot(config, history, at(1))
    again = snapshot(config, history, at(2))

    assert first is not None
    assert again is None
    assert len(snapshots(history)) == 1


def test_changed_config_appends_a_snapshot(tmp_path: Path) -> None:
    config = write_config(tmp_path, "models:\n  default: qwen\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))

    config.write_text("models:\n  default: opus\n")
    snapshot(config, history, at(2))

    kept = snapshots(history)
    assert [path.read_text() for path in kept] == [
        "models:\n  default: qwen\n",
        "models:\n  default: opus\n",
    ]


def test_reverting_to_an_earlier_config_snapshots_it_again(tmp_path: Path) -> None:
    """Dedup compares only against the newest snapshot, not the whole
    directory: going back to an old value is a change like any other, and the
    newest file must always be what is live."""
    config = write_config(tmp_path, "a\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    config.write_text("b\n")
    snapshot(config, history, at(2))

    config.write_text("a\n")
    snapshot(config, history, at(3))

    assert [path.read_text() for path in snapshots(history)] == ["a\n", "b\n", "a\n"]


def test_history_is_trimmed_to_the_newest_kept(tmp_path: Path) -> None:
    config = write_config(tmp_path, "seed\n")
    history = tmp_path / "history"
    for second in range(KEEP + 5):
        config.write_text(f"value: {second}\n")
        snapshot(config, history, at(second))

    kept = snapshots(history)
    assert len(kept) == KEEP
    # The oldest are the ones dropped; the newest is what is live now.
    assert kept[0].read_text() == f"value: {KEEP + 5 - KEEP}\n"
    assert kept[-1].read_text() == f"value: {KEEP + 4}\n"


def test_missing_config_snapshots_nothing(tmp_path: Path) -> None:
    """No config.yaml is a real state (a fresh clone, a test rig) and must not
    litter the history or raise on the boot path."""
    history = tmp_path / "history"

    assert snapshot(tmp_path / "config.yaml", history, at(1)) is None
    # Not `snapshots(history) == []` — that is also true of a directory that
    # was created and left empty, which is the littering this rules out.
    assert not history.exists()


def test_snapshot_does_not_widen_the_config_permissions(tmp_path: Path) -> None:
    """An owner who reacts to "this file holds my phone number" by narrowing
    config.yaml must not find a world-readable copy of it in the history."""
    config = write_config(tmp_path, "imessage:\n  owner_handles: ['+15551234567']\n")
    config.chmod(0o600)
    history = tmp_path / "history"

    written = snapshot(config, history, at(1))

    assert written is not None
    assert written.stat().st_mode & 0o777 == 0o600


def test_a_future_stamped_entry_cannot_unlink_the_live_snapshot(
    tmp_path: Path,
) -> None:
    """A box that boots with its clock set ahead leaves entries that sort last
    forever. Once KEEP of them exist, a trim that did not exempt the file it
    just wrote would delete the very config that is live."""
    config = write_config(tmp_path, "live\n")
    history = tmp_path / "history"
    history.mkdir()
    for n in range(KEEP + 2):
        (history / f"2099-01-01T00-00-{n:02d}Z.yaml").write_text(f"future {n}\n")

    written = snapshot(config, history, at(1))

    assert written is not None
    assert written.read_text() == "live\n"
    kept = snapshots(history)
    assert written in kept
    # Exempting it costs one entry over the cap in this pathological case,
    # which is a far better trade than unlinking the config that is live.
    assert len(kept) == KEEP + 1


def test_snapshot_compares_bytes_not_text(tmp_path: Path) -> None:
    """A non-UTF-8 byte in a hand-edited config must not raise here — reading
    the vault taught this lesson once already (#220)."""
    config = tmp_path / "config.yaml"
    config.write_bytes(b"note: caf\xe9\n")
    history = tmp_path / "history"

    written = snapshot(config, history, at(1))

    assert written is not None
    assert written.read_bytes() == b"note: caf\xe9\n"
    assert snapshot(config, history, at(2)) is None


def test_record_config_survives_an_unwritable_history_dir(tmp_path: Path) -> None:
    """The boot calls this once it is already proven healthy, so a filesystem
    problem here must be logged and dropped — never turned into a crash that
    the rollback path would then read as a bad self-edit."""
    from chief.entrypoint import record_config

    config = write_config(tmp_path, "models:\n  default: qwen\n")
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the data dir should be")

    record_config(config, blocked)  # must not raise


def test_record_config_writes_under_the_data_dir(tmp_path: Path) -> None:
    from chief.entrypoint import record_config

    config = write_config(tmp_path, "models:\n  default: qwen\n")
    data_dir = tmp_path / "data"

    record_config(config, data_dir)

    assert [path.name for path in snapshots(data_dir / "config-history")] != []


def test_snapshots_are_returned_oldest_first(tmp_path: Path) -> None:
    """Order is the contract: name order is write order, so the last entry is
    the last config that booted — which is what the recovery restores."""
    config = write_config(tmp_path, "one\n")
    history = tmp_path / "history"
    snapshot(config, history, at(1))
    config.write_text("two\n")
    snapshot(config, history, at(2))
    config.write_text("three\n")
    snapshot(config, history, at(3))

    # The whole list, not just index -2: with three entries a reversed sort
    # still puts "two" in the middle, so indexing alone would pass either way.
    assert [path.read_text() for path in snapshots(history)] == [
        "one\n",
        "two\n",
        "three\n",
    ]
