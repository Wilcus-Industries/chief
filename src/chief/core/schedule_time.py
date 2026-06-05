"""Pure timing helpers for the scheduler (M9a) — no I/O, easy to unit-test.

The tick loop leans on three calculations, all kept here so the engine
(:mod:`chief.core.scheduler`) stays about orchestration:

- :func:`next_fire` — when a schedule fires next (``once`` ⇒ its own ISO timestamp;
  ``recurring`` ⇒ the next cron occurrence).
- :func:`in_quiet_hours` — is a moment inside the owner's quiet window.
- :func:`defer_target` — when a quiet-deferred fire releases (the next ``quiet_end``).

Schedules are stored in UTC, but the owner thinks in local time: cron expressions and
quiet-hours windows are interpreted in ``owner_tz`` and then converted back. Every
datetime here flows in and out UTC-aware; callers stamp UTC on the naive values sqlite
hands back (see :mod:`chief.persistence.rate_limits`).
"""

from datetime import UTC, datetime, time, timedelta, tzinfo

from croniter import croniter

from ..persistence.schedules import KIND_ONCE, KIND_RECURRING


def _parse_hhmm(value: str) -> time:
    """Parse a ``"HH:MM"`` wall-clock string into a :class:`~datetime.time`."""
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def next_fire(
    kind: str, spec: str, *, after: datetime, tz: tzinfo
) -> datetime | None:
    """Return the next UTC fire time for ``spec``, or ``None`` if it never fires.

    ``once`` parses ``spec`` as an ISO-8601 timestamp; a naive value is read as the
    owner's wall clock (stamped with ``tz``). It returns that timestamp *regardless of*
    ``after`` (even if already past) — a one-off never re-advances, so the tick treats
    a past ``next_run`` as due then disables it (``list_due`` is ``<=`` now).

    ``recurring`` reads ``spec`` as a cron expression evaluated in ``tz`` from ``after``
    (so DST shifts land on the right wall clock). ``after`` must be UTC-aware; an
    unknown ``kind`` returns ``None``.
    """
    if kind == KIND_ONCE:
        fire = datetime.fromisoformat(spec)
        if fire.tzinfo is None:
            fire = fire.replace(tzinfo=tz)
        return fire.astimezone(UTC)
    if kind == KIND_RECURRING:
        local_after = after.astimezone(tz)
        # croniter is untyped (get_next returns Any) — annotate to keep mypy strict.
        nxt: datetime = croniter(spec, local_after).get_next(datetime)
        return nxt.astimezone(UTC)
    return None


def in_quiet_hours(
    now: datetime, start: str | None, end: str, tz: tzinfo
) -> bool:
    """True iff ``now`` (UTC-aware) falls inside the owner's quiet window.

    ``start``/``end`` are ``"HH:MM"`` wall-clock strings in ``tz``. A ``None`` start
    disables quiet hours (always ``False``). The window is half-open ``[start, end)``
    and handles the overnight wrap (``22:00``→``07:00`` spans midnight).
    """
    if start is None:
        return False
    local = now.astimezone(tz).time()
    begin = _parse_hhmm(start)
    finish = _parse_hhmm(end)
    if begin <= finish:
        return begin <= local < finish
    # Overnight: inside if at/after start OR before end.
    return local >= begin or local < finish


def defer_target(now: datetime, quiet_end: str, tz: tzinfo) -> datetime:
    """The UTC moment a quiet-deferred fire is released — the next ``quiet_end``.

    ``quiet_end`` is a ``"HH:MM"`` wall-clock string in ``tz``. Returns that time on
    ``now``'s local day if it is still ahead, else the next day's (DST-correct, since
    the target is rebuilt from the local date rather than offset by 24h).
    """
    end = _parse_hhmm(quiet_end)
    local = now.astimezone(tz)
    target = datetime.combine(local.date(), end, tzinfo=tz)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), end, tzinfo=tz)
    return target.astimezone(UTC)
