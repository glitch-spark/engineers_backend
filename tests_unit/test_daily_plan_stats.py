"""Unit tests for the regular daily plan chart buckets."""

import os
from datetime import date, datetime
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.daily_plan_stats import build_series, bucket_key, sum_totals, window_start


def _plan(day: date, **kw):
    base = dict(
        date=datetime(day.year, day.month, day.day),
        bidsHandsOn=0,
        bidsByBidder=0,
        interviewsDone=0,
        interviewsNew=0,
        customItems=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _item(label: str, value: int):
    return SimpleNamespace(label=label, value=value)


def test_day_window_covers_fourteen_days():
    today = date(2026, 9, 27)
    assert window_start(today, "day", 14) == date(2026, 9, 14)
    series = build_series([], today, "day", 14)
    assert len(series) == 14
    assert series[0]["bucketStart"] == "2026-09-14"
    assert series[-1]["bucketEnd"] == "2026-09-27"


def test_month_window_crosses_the_year():
    assert window_start(date(2026, 3, 15), "month", 12) == date(2025, 4, 1)
    series = build_series([], date(2026, 3, 15), "month", 12)
    assert [r["key"] for r in series][:2] == ["2025-04", "2025-05"]
    assert series[-1]["key"] == "2026-03"
    assert series[-1]["bucketEnd"] == "2026-03-15"


def test_weeks_start_on_sunday_like_the_report_filter():
    # 2026-09-27 is a Sunday, so it opens a new app week.
    assert bucket_key(date(2026, 9, 26), "week") != bucket_key(date(2026, 9, 27), "week")
    assert bucket_key(date(2026, 9, 27), "week") == bucket_key(date(2026, 10, 3), "week")
    assert window_start(date(2026, 9, 30), "week", 2) == date(2026, 9, 20)


def test_weeks_restart_on_jan_first():
    # Wed 2025-12-31 and Thu 2026-01-01 share a Sun–Sat span but not an app week.
    assert bucket_key(date(2025, 12, 31), "week") == "2025-W53"
    assert bucket_key(date(2026, 1, 1), "week") == "2026-W01"
    series = build_series([], date(2026, 1, 2), "week", 2)
    assert [(r["bucketStart"], r["bucketEnd"]) for r in series] == [
        ("2025-12-28", "2025-12-31"),
        ("2026-01-01", "2026-01-02"),
    ]


def test_series_sums_plans_into_their_bucket_and_skips_outside_window():
    today = date(2026, 9, 27)
    plans = [
        _plan(date(2026, 9, 1), bidsHandsOn=4, bidsByBidder=10, interviewsDone=1, interviewsNew=2),
        _plan(date(2026, 9, 20), bidsHandsOn=1, bidsByBidder=5, interviewsNew=1),
        _plan(date(2026, 8, 31), bidsHandsOn=100),
        _plan(date(2026, 9, 28), bidsHandsOn=100),  # after `today`
    ]
    series = build_series(plans, today, "month", 2)
    assert [r["key"] for r in series] == ["2026-08", "2026-09"]
    aug, sep = series
    assert aug["bidsHandsOn"] == 100 and aug["planCount"] == 1
    assert sep["bidsHandsOn"] == 5
    assert sep["bidsByBidder"] == 15
    assert sep["interviewsDone"] == 1
    assert sep["interviewsNew"] == 3
    assert sep["planCount"] == 2


def test_totals_merge_custom_items_by_label():
    today = date(2026, 9, 27)
    plans = [
        _plan(date(2026, 9, 26), bidsByBidder=3, customItems=[_item("Cold emails", 5), _item("Calls", 1)]),
        _plan(date(2026, 9, 27), bidsByBidder=2, customItems=[_item("cold emails ", 2)]),
        _plan(date(2026, 1, 1), customItems=[_item("Calls", 50)]),  # outside the window
    ]
    series = build_series(plans, today, "day", 14)
    totals = sum_totals(series, plans, date(2026, 9, 14), today)
    assert totals["bidsByBidder"] == 5
    assert totals["planCount"] == 2
    assert totals["customItems"] == [
        {"label": "Cold emails", "value": 7},
        {"label": "Calls", "value": 1},
    ]
