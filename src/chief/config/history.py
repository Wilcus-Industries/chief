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

from datetime import datetime
from pathlib import Path

#: Snapshots kept before the oldest are trimmed. Config moves rarely (this
#: only writes on a *change*), so this is many changes deep, not many days.
KEEP = 20

#: Colons are not portable in filenames; dashes keep the stamp sortable, which
#: is what makes plain name order the same as write order.
STAMP_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def snapshots(history_dir: Path) -> list[Path]:
    """Every snapshot, oldest first. The last entry is what is live now, so
    the previous config is the second-to-last."""
    if not history_dir.is_dir():
        return []
    return sorted(history_dir.glob("*.yaml"))


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
    written.write_bytes(current)
    written.chmod(config_path.stat().st_mode & 0o777)
    # Skip the file just written: a box that boots with its clock set ahead
    # leaves a future-stamped entry sorting last forever, and once KEEP of
    # them exist this loop would otherwise unlink the live snapshot.
    for stale in snapshots(history_dir)[:-KEEP]:
        if stale != written:
            stale.unlink()
    return written
