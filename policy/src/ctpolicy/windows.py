"""Compute-window arithmetic.

A window is a recurring local-time interval on a set of weekdays, e.g.
`Mon-Fri 08:00-20:00`. Rather than reason about weekday wrap-around, DST and
adjacency inline, every query materializes the windows into concrete datetime
intervals over a short horizon around the instant being asked about, merges
them, and answers from that list. The horizon is a little over a week, so weekly
recurrence is always fully covered, and the interval count stays trivially small.

A window whose end is not after its start wraps past midnight; its `days` name
the day it *starts* on. `24:00` is accepted as an end and means the end of the
day, which is the non-wrapping way to say "until midnight".
"""

from datetime import date, datetime, timedelta, tzinfo
from typing import Optional

# Indexed by `date.weekday()`.
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SECONDS_PER_DAY = 24 * 3600

Interval = tuple[datetime, datetime]

# One day back so an in-progress wrapped window is visible, plus a full week
# forward so `next_open` always finds the next occurrence of any weekly window.
_HORIZON_BACK_DAYS = 1
_HORIZON_FORWARD_DAYS = 8


def is_open(intervals: list[Interval], now: datetime) -> bool:
    return any(start <= now < end for start, end in intervals)


def current_close(intervals: list[Interval], now: datetime) -> Optional[datetime]:
    """When the currently open span ends, or `None` if nothing is open."""
    for start, end in intervals:
        if start <= now < end:
            return end
    return None


def next_open(intervals: list[Interval], now: datetime) -> Optional[datetime]:
    """When the next span starts, or `None` if none starts within the horizon."""
    for start, _ in intervals:
        if start > now:
            return start
    return None


def seconds_until_close(intervals: list[Interval], now: datetime) -> Optional[int]:
    close = current_close(intervals, now)
    if close is None:
        return None
    return max(0, int((close - now).total_seconds()))


def materialize(windows: list["WindowLike"], tz: tzinfo, around: datetime) -> list[Interval]:
    """Expand recurring windows into merged, sorted concrete intervals."""
    first_day = (around.astimezone(tz) - timedelta(days=_HORIZON_BACK_DAYS)).date()
    intervals: list[Interval] = []
    for offset in range(_HORIZON_BACK_DAYS + _HORIZON_FORWARD_DAYS):
        day = first_day + timedelta(days=offset)
        for window in windows:
            if day.weekday() not in window.weekdays:
                continue
            intervals.append(_occurrence(window, day, tz))
    return _merge(intervals)


def _occurrence(window: "WindowLike", day: date, tz: tzinfo) -> Interval:
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    start = midnight + timedelta(seconds=window.start_seconds)
    end_seconds = window.end_seconds
    if end_seconds <= window.start_seconds:
        # Wraps past midnight; `days` names the start day.
        end_seconds += SECONDS_PER_DAY
    return start, midnight + timedelta(seconds=end_seconds)


def _merge(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping and touching intervals so an open span is contiguous.

    Touching intervals are merged too: back-to-back windows (say 08:00-12:00 and
    12:00-20:00) are one continuous span, and reporting them separately would
    make `seconds_until_close` truncate a run at noon for no reason.
    """
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


class WindowLike:
    """Structural type for what `materialize` needs from a window.

    Declared for type checkers only; `ctpolicy.config.Window` is the real model.
    Windows are expressed in seconds-from-midnight rather than `time` objects so
    that `24:00` — which is not a valid `time` — is representable as an end.
    """

    weekdays: frozenset[int]
    start_seconds: int
    end_seconds: int
