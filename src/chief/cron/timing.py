"""Pure schedule timing: next-fire computation and quiet-hours deferral.

A spec is either standard 5-field cron or ``@every <seconds>`` for intervals
(seconds granularity keeps tests fast and covers "every N minutes" errands).
"""

from datetime import UTC, datetime, time, timedelta

from croniter import croniter

QuietHours = tuple[time, time]


def parse_quiet_hours(raw: str) -> QuietHours | None:
    """Parse "HH:MM-HH:MM" (empty string means no quiet hours)."""
    if not raw:
        return None
    start_raw, end_raw = raw.split("-")
    return time.fromisoformat(start_raw), time.fromisoformat(end_raw)


def next_fire(spec: str, after: datetime, quiet: QuietHours | None) -> datetime:
    """When a schedule fires next, deferred out of the quiet window."""
    if spec.startswith("@every "):
        fire = after + timedelta(seconds=float(spec.removeprefix("@every ")))
    else:
        fire = croniter(spec, after).get_next(datetime)
    return defer_quiet(fire, quiet)


def defer_quiet(fire: datetime, quiet: QuietHours | None) -> datetime:
    """Push a fire time inside the quiet window to the window's end."""
    if quiet is None:
        return fire
    start, end = quiet
    moment = fire.timetz().replace(tzinfo=None)
    if start <= end:
        inside = start <= moment < end
        end_day = fire
    else:  # overnight window, e.g. 23:00-08:00
        inside = moment >= start or moment < end
        end_day = fire + timedelta(days=1) if moment >= start else fire
    if not inside:
        return fire
    return end_day.replace(
        hour=end.hour, minute=end.minute, second=0, microsecond=0
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
