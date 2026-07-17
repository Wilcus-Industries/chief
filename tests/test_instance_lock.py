"""Single-instance flock: a second daemon on the same data dir is refused."""

from pathlib import Path

import pytest

from chief.instance_lock import AlreadyRunning, acquire_instance_lock


def test_second_acquire_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "chief.lock"
    held = acquire_instance_lock(lock)
    with pytest.raises(AlreadyRunning):
        acquire_instance_lock(lock)
    held.close()


def test_release_lets_the_next_acquire_succeed(tmp_path: Path) -> None:
    lock = tmp_path / "chief.lock"
    first = acquire_instance_lock(lock)
    first.close()  # a dead daemon's fd closing drops the lock
    second = acquire_instance_lock(lock)  # must not raise
    second.close()


def test_creates_the_data_dir(tmp_path: Path) -> None:
    lock = tmp_path / "data" / "chief.lock"
    held = acquire_instance_lock(lock)
    assert lock.exists()
    held.close()
