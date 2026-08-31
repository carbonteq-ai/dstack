from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ctpolicy import windows
from ctpolicy.config import Window

UTC = ZoneInfo("UTC")


def window(days, start, end) -> Window:
    return Window.parse_obj({"days": days, "from": start, "to": end})


MONDAY = datetime(2026, 8, 31, tzinfo=UTC)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A datetime in the week starting Monday 2026-08-31; `day` 1 is that Monday."""
    return MONDAY + timedelta(days=day - 1, hours=hour, minutes=minute)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


class TestIsOpen:
    def test_inside_window(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 12)
        assert windows.is_open(windows.materialize(w, UTC, now), now)

    def test_before_window(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 7, 59)
        assert not windows.is_open(windows.materialize(w, UTC, now), now)

    def test_start_is_inclusive(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 8, 0)
        assert windows.is_open(windows.materialize(w, UTC, now), now)

    def test_end_is_exclusive(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 20, 0)
        assert not windows.is_open(windows.materialize(w, UTC, now), now)

    def test_excluded_weekday(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(6, 12)  # Saturday
        assert not windows.is_open(windows.materialize(w, UTC, now), now)


class TestMidnight:
    def test_to_24_00_stays_open_until_midnight(self):
        w = [window(["mon"], "22:00", "24:00")]
        now = at(1, 23, 59)
        assert windows.is_open(windows.materialize(w, UTC, now), now)

    def test_to_24_00_does_not_leak_into_the_next_day(self):
        w = [window(["mon"], "22:00", "24:00")]
        now = at(2, 0, 1)
        assert not windows.is_open(windows.materialize(w, UTC, now), now)

    def test_wrapping_window_covers_the_early_hours_of_the_next_day(self):
        # `days` names the day the window STARTS on.
        w = [window(["mon"], "20:00", "06:00")]
        assert windows.is_open(windows.materialize(w, UTC, at(1, 23)), at(1, 23))
        assert windows.is_open(windows.materialize(w, UTC, at(2, 5)), at(2, 5))
        assert not windows.is_open(windows.materialize(w, UTC, at(2, 7)), at(2, 7))

    def test_wrapping_window_is_visible_from_inside_the_next_day(self):
        """The horizon reaches back a day, or an in-progress wrap would look shut."""
        w = [window(["mon"], "20:00", "06:00")]
        now = at(2, 2)
        assert windows.is_open(windows.materialize(w, UTC, now), now)


class TestCloseAndNextOpen:
    def test_seconds_until_close(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 19, 30)
        assert windows.seconds_until_close(windows.materialize(w, UTC, now), now) == 30 * 60

    def test_seconds_until_close_is_none_when_shut(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(6, 12)
        assert windows.seconds_until_close(windows.materialize(w, UTC, now), now) is None

    def test_adjacent_windows_merge_into_one_span(self):
        """Back-to-back windows must not truncate a run at the seam."""
        w = [window(["mon"], "08:00", "12:00"), window(["mon"], "12:00", "20:00")]
        now = at(1, 11, 0)
        assert windows.seconds_until_close(windows.materialize(w, UTC, now), now) == 9 * 3600

    def test_overlapping_windows_merge(self):
        w = [window(["mon"], "08:00", "14:00"), window(["mon"], "12:00", "18:00")]
        now = at(1, 13)
        assert windows.seconds_until_close(windows.materialize(w, UTC, now), now) == 5 * 3600

    def test_next_open_is_the_following_morning(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(1, 21)
        assert windows.next_open(windows.materialize(w, UTC, now), now) == at(2, 8)

    def test_next_open_skips_the_weekend(self):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(6, 12)  # Saturday
        assert windows.next_open(windows.materialize(w, UTC, now), now) == at(8, 8)

    @pytest.mark.parametrize("day", [1, 2, 3, 4, 5])
    def test_every_weekday_has_an_occurrence(self, day):
        w = [window(WEEKDAYS, "08:00", "20:00")]
        now = at(day, 12)
        assert windows.is_open(windows.materialize(w, UTC, now), now)


class TestNonUtcTimezone:
    def test_window_is_evaluated_in_the_configured_zone(self):
        karachi = ZoneInfo("Asia/Karachi")  # UTC+5, no DST
        w = [window(["mon"], "08:00", "20:00")]
        # 04:00 UTC on Monday is 09:00 in Karachi: open.
        now = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
        assert windows.is_open(windows.materialize(w, karachi, now), now)
        # 16:00 UTC is 21:00 in Karachi: shut.
        now = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
        assert not windows.is_open(windows.materialize(w, karachi, now), now)
