"""Unit tests for Slack bot reminder window / digest slot helpers."""

import os
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.slack_bot import digest_slot_open, reminder_window


def test_reminder_window_centered_on_offset():
    now = datetime(2026, 7, 28, 12, 0, 0)
    start, end = reminder_window(now, minutes=30, skew_seconds=90)
    assert start == datetime(2026, 7, 28, 12, 28, 30)
    assert end == datetime(2026, 7, 28, 12, 31, 30)


def test_digest_slot_open_inside_window():
    now = datetime(2026, 7, 28, 8, 2, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is True


def test_digest_slot_open_outside_window():
    now = datetime(2026, 7, 28, 8, 6, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is False
    now2 = datetime(2026, 7, 28, 7, 59, 0)
    assert digest_slot_open(now2, hour=8, minute=0, window_minutes=5) is False
