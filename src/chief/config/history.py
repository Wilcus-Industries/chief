"""Timestamped copies of every ``config.yaml`` that booted healthy.

The self-edit seatbelt is git, and git can only restore what it tracks.
``config.yaml`` is gitignored — it holds ``owner_handles`` and this repo is
published, so tracking it would push the owner's handles upstream — which
leaves a bad config write with **no undo at all**: it is absent from
``git status``, never enters the commit, and ``git reset --hard`` steps right
past it.

This is the recovery path git cannot provide, kept deliberately dumber than
git: plain files in a directory, so ``ls`` is the history, ``diff`` is the
diff, and ``cp`` is the restore. No repo, no index, nothing to learn.

Written where the boot healthcheck passes, so the directory holds only configs
that actually came all the way up — a config too broken to boot never enters
the history it would have to be recovered from. A snapshot is skipped when the
content matches the newest one already there, which makes the directory a
record of every *change* rather than of every restart.
"""

import os
from datetime import datetime
from pathlib import Path

#: Snapshots kept before the oldest are trimmed. Config moves rarely (this
#: only writes on a *change*), so this is many changes deep, not many days.
KEEP = 20

#: Colons are not portable in filenames; dashes keep the stamp sortable, which
#: is what makes plain name order the same as write order.
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def snapshots(history_dir: Path) -> list[Path]:
    """Every snapshot, oldest first.

    Name order is write order (see :data:`STAMP_FORMAT`), and only a config
    that booted is ever written here — so the last entry is the last config
    known to come up, which is what :func:`restore_newest` puts back.
    """
    if not history_dir.is_dir():
        return []
    return sorted(history_dir.glob("*.yaml"))


def _write_private(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` at ``mode``, never wider and never partial.

    Created at 0600 rather than written and chmod'd after: these files hold
    ``owner_handles``, and the plain write would leave one world-readable at
    the umask default for the breath in between. Renamed into place because
    the restore overwrites the live config at the moment the rollback marker
    is already gone — a crash mid-write there would leave a truncated config
    and nothing left to undo it with.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    tmp.chmod(mode)
    os.replace(tmp, path)


def restore_newest(
    history_dir: Path, config_path: Path, failed_copy: Path
) -> Path | None:
    """Put the newest snapshot back, keeping the config it replaced.

    The boot-failure undo for a config change, since git cannot roll back a
    file it does not track. The **newest** entry is right here, not the
    second-to-last: a config that failed to boot never got snapshotted, so
    the last entry is still the last one that came up.

    The config being replaced is saved to ``failed_copy`` first — it may hold
    an edit the owner still wants, and a failed boot is not a reason to
    discard it silently. Returns the snapshot restored, or ``None`` when
    there is no history or the config already matches it (nothing to undo,
    and the caller must not reboot into an unchanged state).
    """
    kept = snapshots(history_dir)
    if not kept:
        return None
    newest = kept[-1]
    mode = newest.stat().st_mode & 0o777
    if config_path.is_file():
        current = config_path.read_bytes()
        if current == newest.read_bytes():
            return None
        live = config_path.stat().st_mode & 0o777
        failed_copy.parent.mkdir(parents=True, exist_ok=True)
        _write_private(failed_copy, current, live)
        # The narrower of the two: an owner who ran `chmod 600 config.yaml`
        # must not have it widened again by a snapshot taken before they did.
        mode &= live
    _write_private(config_path, newest.read_bytes(), mode)
    return newest


def snapshot(config_path: Path, history_dir: Path, now: datetime) -> Path | None:
    """Copy ``config_path`` into the history, unless it is already the newest.

    Returns the file written, or ``None`` when there was no config to copy or
    it had not changed. ``now`` is passed in rather than read so the caller
    (and the tests) control the stamp.

    Bytes, not text: a hand-edited config carrying a stray non-UTF-8 byte must
    not raise on the boot path (#220).
    """
    if not config_path.is_file():
        return None
    current = config_path.read_bytes()
    kept = snapshots(history_dir)
    if kept and kept[-1].read_bytes() == current:
        return None
    history_dir.mkdir(parents=True, exist_ok=True)
    # ponytail: same-second boots collide on the name and the later wins.
    # Two *distinct* configs booting inside one second is not a real case;
    # add a counter suffix if it ever becomes one.
    written = history_dir / f"{now.strftime(STAMP_FORMAT)}.yaml"
    # Write the bytes already read, rather than copying the file again: the
    # snapshot is then exactly what was compared, and it does not inherit
    # ``copyfile``'s mode handling — which drops the source's bits, so a
    # ``chmod 600 config.yaml`` would have yielded a 0644 copy of the handles
    # the owner had just narrowed.
    _write_private(written, current, config_path.stat().st_mode & 0o777)
    # Skip the file just written: a box that boots with its clock set ahead
    # leaves a future-stamped entry sorting last forever, and once KEEP of
    # them exist this loop would otherwise unlink the live snapshot.
    for stale in snapshots(history_dir)[:-KEEP]:
        if stale != written:
            stale.unlink()
    return written
