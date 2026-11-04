"""Profile metrics window: the completed Monday-Sunday weeks measured, and the Bid Track day a bid belongs to."""
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.bids.windows import BidSchedule
from app.profile_metrics import bid_report_day, span

TODAY = date(2026, 11, 4)
LONG_AGO = date(2025, 1, 1)
SCHED = BidSchedule(tz=ZoneInfo("America/Chicago"), daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def test_span_full_window():
    assert span(4, TODAY, date(2026, 1, 1), LONG_AGO) == (date(2026, 10, 5), date(2026, 11, 1), 4)


def test_span_since_midweek():
    assert span(4, TODAY, date(2026, 10, 21), LONG_AGO) == (date(2026, 10, 26), date(2026, 11, 1), 1)


def test_span_since_monday():
    assert span(4, TODAY, date(2026, 10, 19), LONG_AGO) == (date(2026, 10, 19), date(2026, 11, 1), 2)


def test_span_created_midweek():
    assert span(4, TODAY, date(2026, 1, 1), date(2026, 10, 28)) == (date(2026, 10, 26), date(2026, 11, 1), 1)


def test_span_no_completed_week():
    assert span(4, TODAY, date(2026, 11, 3), LONG_AGO)[2] == 0


def test_bid_report_day_cutoff():
    assert bid_report_day(datetime(2026, 10, 20, 23, 29), SCHED) == date(2026, 10, 20)
    assert bid_report_day(datetime(2026, 10, 20, 23, 30), SCHED) == date(2026, 10, 21)
