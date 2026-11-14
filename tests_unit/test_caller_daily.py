"""When the daily post is due (05:00 America/Chicago) and the day it covers; the cron entrypoint that runs it."""

import asyncio
import os
from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.slack_bot import caller_daily_bounds, caller_daily_due  # noqa: E402

CHI = ZoneInfo("America/Chicago")
FIVE = time(5, 0)
DAY = date(2026, 10, 7)


def chi(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=CHI)


# --- due / bounds ---------------------------------------------------------------------------------


def test_due_only_after_five_chicago():
    assert caller_daily_due(chi(DAY, 4, 59), at=FIVE, tz=CHI) is None
    assert caller_daily_due(chi(DAY, 5, 0), at=FIVE, tz=CHI) == DAY
    assert caller_daily_due(chi(DAY, 23, 59), at=FIVE, tz=CHI) == DAY  # catch-up the rest of the day
    assert caller_daily_due(chi(DAY, 5, 0).astimezone(timezone.utc), at=FIVE, tz=CHI) == DAY  # UTC input


def test_bounds_are_local_midnights():
    start, end = caller_daily_bounds(DAY, CHI)
    assert (start, end) == (datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc), datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc))


def test_bounds_on_dst_day_are_25_hours():
    start, end = caller_daily_bounds(date(2026, 11, 1), CHI)
    assert start == datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)


# --- cron order -------------------------------------------------------------------------------


def test_alert_tick_runs_day_post_after_reminders():
    order = []
    reminders = AsyncMock(side_effect=lambda now: order.append("reminders") or {"sent": 0, "failed": 0})
    day_post = AsyncMock(side_effect=lambda now: order.append("day_post") or "posted")
    with (
        patch("app.interview_day.reminders.run_round_reminders", reminders),
        patch("app.interview_day.post.run_day_post", day_post),
        patch.object(slack_bot, "slack_bot_configured", lambda: False),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert order == ["reminders", "day_post"] and out["caller_daily"] == "posted"


def test_alert_tick_sends_no_dms():
    send = AsyncMock(return_value=True)
    with (
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 2, "failed": 0})),
        patch("app.interview_day.post.run_day_post", AsyncMock(return_value="posted")),
        patch.object(slack_bot, "slack_bot_configured", lambda: True),
        patch.object(slack_bot, "send_dm", send),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out == {"caller_reminders": 2, "caller_daily": "posted"}
    send.assert_not_awaited()


def test_alert_tick_survives_day_post_crash():
    with (
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 0, "failed": 0})),
        patch("app.interview_day.post.run_day_post", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(slack_bot, "slack_bot_configured", lambda: False),
    ):
        assert asyncio.run(slack_bot.run_alert_tick())["caller_daily"] == "failed"


def test_alert_tick_survives_reminder_crash():
    day_post = AsyncMock(return_value="posted")
    with (
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(side_effect=RuntimeError("boom"))),
        patch("app.interview_day.post.run_day_post", day_post),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out == {"caller_reminders": 0, "caller_daily": "posted"}
    day_post.assert_awaited_once()
