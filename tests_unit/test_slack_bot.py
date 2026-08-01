"""Unit tests for Slack digest helpers and Know your why."""

import os
from datetime import date, datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.know_your_why import know_your_why_for_date
from app.slack_bot import calendar_day_bounds, digest_slot_open, validate_timezone
from app.slack_timezones import SLACK_DIGEST_TIMEZONES, normalize_slack_timezone


def test_digest_slot_open_inside_window():
    now = datetime(2026, 7, 28, 8, 2, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is True


def test_digest_slot_open_outside_window():
    now = datetime(2026, 7, 28, 8, 6, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is False
    now2 = datetime(2026, 7, 28, 7, 59, 0)
    assert digest_slot_open(now2, hour=8, minute=0, window_minutes=5) is False


def test_calendar_day_bounds():
    start, end = calendar_day_bounds(date(2026, 7, 28))
    assert start == datetime(2026, 7, 28, 0, 0, 0)
    assert end == datetime(2026, 7, 29, 0, 0, 0)


def test_validate_timezone_ok():
    assert validate_timezone("Asia/Hong_Kong") == "Asia/Hong_Kong"
    assert validate_timezone("America/New_York") == "America/New_York"


def test_validate_timezone_invalid():
    try:
        validate_timezone("Asia/Famagusta")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid timezone" in str(exc)


def test_normalize_unknown_falls_back():
    assert normalize_slack_timezone("Asia/Famagusta") == "America/New_York"


def test_catalog_has_27_hourly_offsets():
    assert len(SLACK_DIGEST_TIMEZONES) == 27


def test_know_your_why_stable_for_date():
    a = know_your_why_for_date("2026-07-28")
    b = know_your_why_for_date(date(2026, 7, 28))
    assert a == b
    assert isinstance(a, str) and len(a) > 10
