"""Unit tests for the pure dashboard helpers (week window, buckets, pace, streak, percentile)."""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest

from app.dashboard_stats import (
    bucket_for,
    bucket_keys,
    bucket_label,
    local_day_start_utc,
    month_keys,
    pace,
    percentile,
    pick_bucket,
    streak,
    week_window,
)


def test_week_window_midweek_and_sunday():
    assert week_window(date(2026, 10, 1)) == (date(2026, 9, 28), date(2026, 10, 3), date(2026, 10, 4))
    assert week_window(date(2026, 10, 4)) == (date(2026, 9, 28), date(2026, 10, 3), date(2026, 10, 4))


@pytest.mark.parametrize("days,expected", [(1, "day"), (62, "day"), (63, "week"), (182, "week"), (183, "month"), (365, "month")])
def test_pick_bucket_boundaries(days, expected):
    start = date(2026, 1, 1)
    assert pick_bucket(start, start + timedelta(days=days - 1)) == expected


def test_day_keys_skip_sunday_and_sunday_merges_into_saturday():
    keys = bucket_keys(date(2026, 9, 28), date(2026, 10, 4), "day")
    assert len(keys) == 6 and keys[-1] == date(2026, 10, 3)
    assert bucket_for(date(2026, 10, 4), "day", keys) == date(2026, 10, 3)


def test_range_starting_sunday_rolls_forward():
    keys = bucket_keys(date(2026, 10, 4), date(2026, 10, 10), "day")
    assert bucket_for(date(2026, 10, 4), "day", keys) == date(2026, 10, 5)


def test_week_bucket_puts_sunday_in_preceding_week():
    keys = bucket_keys(date(2026, 9, 1), date(2026, 11, 30), "week")
    assert bucket_for(date(2026, 10, 4), "week", keys) == date(2026, 9, 28)


def test_month_labels_same_and_cross_year():
    assert bucket_label(date(2026, 9, 1), "month", date(2026, 1, 1), date(2026, 12, 31)) == "Sep"
    assert bucket_label(date(2026, 9, 1), "month", date(2025, 10, 1), date(2026, 9, 30)) == "Sep 26"


def test_day_label_weekday_for_week_range():
    assert bucket_label(date(2026, 9, 28), "day", date(2026, 9, 28), date(2026, 10, 4)) == "Mon"
    assert bucket_label(date(2026, 9, 28), "day", date(2026, 9, 1), date(2026, 9, 30)) == "Sep 28"


def test_pace_midweek_behind():
    # Wednesday: elapsed 3 of 6, target 50 -> expected 25
    assert pace(20, 50, date(2026, 9, 30)) == {"expected": 25, "behindBy": 5, "perDayNeeded": 10, "onTrack": False}


def test_pace_saturday_and_sunday_have_no_days_left():
    assert pace(50, 50, date(2026, 10, 3))["perDayNeeded"] is None
    assert pace(40, 50, date(2026, 10, 4)) == {"expected": 50, "behindBy": 10, "perDayNeeded": None, "onTrack": False}


def test_pace_no_target():
    assert pace(10, None, date(2026, 9, 30)) is None
    assert pace(10, 0, date(2026, 9, 30)) is None


def test_streak_spans_sunday_and_ignores_missing_today():
    days = {date(2026, 10, 1), date(2026, 9, 30), date(2026, 9, 29), date(2026, 9, 28), date(2026, 9, 26)}
    assert streak(days, date(2026, 10, 2)) == 5  # Fri not logged yet; Thu..Mon + Sat (skip Sun 27)


def test_streak_broken_by_gap_and_zero():
    assert streak({date(2026, 10, 1), date(2026, 9, 29)}, date(2026, 10, 1)) == 1
    assert streak(set(), date(2026, 10, 1)) == 0


def test_percentile_top_bottom_ties_and_small_team():
    assert percentile(50, [50, 40, 30, 20, 10]) == {"position": "top", "percent": 20}
    assert percentile(20, [50, 40, 30, 20, 10]) == {"position": "bottom", "percent": 40}
    assert percentile(40, [40, 40, 10, 5]) == {"position": "top", "percent": 25}
    assert percentile(5, [5, 3]) is None


def test_month_keys_rolls_over_year():
    keys = month_keys(date(2026, 9, 30))
    assert keys[0] == "2025-10" and keys[-1] == "2026-09" and len(keys) == 12


def test_local_day_start_utc_new_york():
    assert local_day_start_utc(date(2026, 9, 28), ZoneInfo("America/New_York")) == datetime(2026, 9, 28, 4, 0)
