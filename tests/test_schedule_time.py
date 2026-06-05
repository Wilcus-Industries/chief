"""Pure scheduler timing helpers (chief.core.schedule_time): no I/O.

Owner tz is America/New_York throughout so the wall-clock reasoning (cron schedules
and quiet-hours windows interpreted locally, then converted to UTC) is exercised at a
non-zero, DST-bearing offset. June dates are EDT (-04:00); March 8, 2026 is the US
spring-forward boundary.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from chief.core.schedule_time import defer_target, in_quiet_hours, next_fire
from chief.persistence.schedules import KIND_ONCE, KIND_RECURRING

NY = ZoneInfo("America/New_York")


def test_next_fire_once_parses_aware_iso_to_utc() -> None:
    after = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    out = next_fire(KIND_ONCE, "2026-06-04T13:30:00+00:00", after=after, tz=NY)
    assert out == datetime(2026, 6, 4, 13, 30, tzinfo=UTC)


def test_next_fire_once_naive_iso_uses_owner_tz() -> None:
    # A naive ISO ts is the owner's wall clock: 09:00 EDT → 13:00 UTC.
    after = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    out = next_fire(KIND_ONCE, "2026-06-04T09:00:00", after=after, tz=NY)
    assert out == datetime(2026, 6, 4, 13, 0, tzinfo=UTC)


def test_next_fire_recurring_cron_in_owner_tz() -> None:
    # "02:00 daily" NY. after = 08:00 EDT Jun 4 → next 02:00 is Jun 5 EDT = 06:00 UTC.
    after = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    out = next_fire(KIND_RECURRING, "0 2 * * *", after=after, tz=NY)
    assert out == datetime(2026, 6, 5, 6, 0, tzinfo=UTC)


def test_next_fire_recurring_every_minute_advances_one() -> None:
    after = datetime(2026, 6, 4, 12, 0, 30, tzinfo=UTC)
    out = next_fire(KIND_RECURRING, "* * * * *", after=after, tz=NY)
    assert out == datetime(2026, 6, 4, 12, 1, tzinfo=UTC)


def test_next_fire_recurring_crosses_dst_spring_forward() -> None:
    # Noon NY daily. after = 13:00 EST Mar 7 (past today's noon) → next noon is Mar 8;
    # clocks sprang forward at 02:00, so noon is EDT (-04:00) = 16:00 UTC (not 17:00).
    after = datetime(2026, 3, 7, 18, 0, tzinfo=UTC)
    out = next_fire(KIND_RECURRING, "0 12 * * *", after=after, tz=NY)
    assert out is not None
    assert out.astimezone(NY).hour == 12
    assert out == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)


def test_next_fire_recurring_bad_spec_returns_none() -> None:
    # A malformed cron expression → None, so the scheduler disables instead of looping.
    after = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    assert next_fire(KIND_RECURRING, "not a cron", after=after, tz=NY) is None


def test_next_fire_unknown_kind_returns_none() -> None:
    after = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    assert next_fire("monitor", "whatever", after=after, tz=NY) is None


def test_in_quiet_hours_overnight_inside_late_night() -> None:
    # 23:00 EDT Jun 4 = 03:00 UTC Jun 5; window 22:00→07:00 NY → inside.
    now = datetime(2026, 6, 5, 3, 0, tzinfo=UTC)
    assert in_quiet_hours(now, "22:00", "07:00", NY) is True


def test_in_quiet_hours_overnight_inside_early_morning() -> None:
    # 05:00 EDT = 09:00 UTC; before 07:00 end → inside.
    now = datetime(2026, 6, 4, 9, 0, tzinfo=UTC)
    assert in_quiet_hours(now, "22:00", "07:00", NY) is True


def test_in_quiet_hours_overnight_outside_daytime() -> None:
    # 12:00 EDT = 16:00 UTC; neither ≥22:00 nor <07:00 → outside.
    now = datetime(2026, 6, 4, 16, 0, tzinfo=UTC)
    assert in_quiet_hours(now, "22:00", "07:00", NY) is False


def test_in_quiet_hours_same_day_window() -> None:
    inside = datetime(2026, 6, 4, 17, 0, tzinfo=UTC)  # 13:00 EDT
    outside = datetime(2026, 6, 4, 19, 0, tzinfo=UTC)  # 15:00 EDT
    assert in_quiet_hours(inside, "12:00", "14:00", NY) is True
    assert in_quiet_hours(outside, "12:00", "14:00", NY) is False


def test_in_quiet_hours_disabled_when_start_none() -> None:
    now = datetime(2026, 6, 4, 9, 0, tzinfo=UTC)
    assert in_quiet_hours(now, None, "07:00", NY) is False


def test_defer_target_next_quiet_end_after_passed() -> None:
    # 23:00 EDT Jun 4; today's 07:00 passed → release at 07:00 EDT Jun 5 = 11:00 UTC.
    now = datetime(2026, 6, 5, 3, 0, tzinfo=UTC)
    out = defer_target(now, "07:00", NY)
    assert out == datetime(2026, 6, 5, 11, 0, tzinfo=UTC)


def test_defer_target_same_morning_when_end_still_ahead() -> None:
    # 02:00 EDT Jun 5; 07:00 still ahead today → release at 07:00 EDT = 11:00 UTC.
    now = datetime(2026, 6, 5, 6, 0, tzinfo=UTC)
    out = defer_target(now, "07:00", NY)
    assert out == datetime(2026, 6, 5, 11, 0, tzinfo=UTC)
