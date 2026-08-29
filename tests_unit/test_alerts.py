"""Unit tests for in-app alert scheduling helpers."""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.alerts import (
    charge_title,
    clamp_billing_date,
    daily_bids_target,
    due_phrase,
    in_reminder_window,
    app_week_key,
    monday_morning_window,
    next_due_date,
    paid_for_period,
    period_key_for_due,
)


def test_charge_title_falls_back_to_last4():
    assert charge_title("", "6400") == "Card ****6400"
    assert charge_title("  Cursor Pro  ", "6400") == "Cursor Pro"


def test_clamp_billing_day_31_in_february():
    assert clamp_billing_date(2026, 2, 31) == date(2026, 2, 28)
    assert clamp_billing_date(2028, 2, 31) == date(2028, 2, 29)


def test_next_due_date_same_month_before_day():
    assert next_due_date(date(2026, 2, 15), 17) == date(2026, 2, 17)


def test_next_due_date_rolls_after_billing_day():
    assert next_due_date(date(2026, 2, 18), 17) == date(2026, 3, 17)


def test_next_due_date_year_wrap():
    assert next_due_date(date(2026, 12, 20), 17) == date(2027, 1, 17)


def test_reminder_window_includes_lead_and_due_day():
    # Due Feb 17 → window Feb 14–17
    assert in_reminder_window(date(2026, 2, 14), 17) is True
    assert in_reminder_window(date(2026, 2, 17), 17) is True
    assert in_reminder_window(date(2026, 2, 13), 17) is False
    assert in_reminder_window(date(2026, 2, 18), 17) is False


def test_period_key_and_paid_for_period():
    due = date(2026, 3, 17)
    assert period_key_for_due(due) == "2026-03"
    assert paid_for_period(date(2026, 3, 15), due) is True
    assert paid_for_period(date(2026, 2, 17), due) is False
    assert paid_for_period(None, due) is False


def test_due_phrase():
    assert due_phrase(0) == "today"
    assert due_phrase(1) == "tomorrow"
    assert due_phrase(3) == "in 3 days"


def test_daily_bids_target_from_weekly():
    assert daily_bids_target(0) == 0
    assert daily_bids_target(10) == 2
    assert daily_bids_target(11) == 3


def test_monday_morning_window():
    tokyo = ZoneInfo("Asia/Tokyo")
    monday_1am = datetime(2026, 8, 24, 1, 0, tzinfo=tokyo)  # Monday
    monday_7am = datetime(2026, 8, 24, 7, 0, tzinfo=tokyo)
    tuesday = datetime(2026, 8, 25, 1, 0, tzinfo=tokyo)
    assert monday_morning_window(monday_1am) is True
    assert monday_morning_window(monday_7am) is False
    assert monday_morning_window(tuesday) is False


def test_app_week_key_matches_spa():
    import math

    local = datetime(2026, 8, 24, 1, 0, tzinfo=ZoneInfo("UTC"))
    year, week, key = app_week_key(local)
    jan1 = date(2026, 1, 1)
    days = (date(2026, 8, 24) - jan1).days
    js_dow = (jan1.weekday() + 1) % 7
    expected = math.ceil((days + js_dow + 1) / 7)
    assert (year, week, key) == (2026, expected, f"2026-W{expected:02d}")
