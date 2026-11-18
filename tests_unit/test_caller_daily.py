"""When the daily post is due (05:00 America/Chicago) and the day it covers; the cron entrypoint that runs it."""

import asyncio
import os
from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.slack_bot import caller_daily_bounds, caller_daily_due  # noqa: E402

CHI = ZoneInfo("America/Chicago")
FIVE = time(5, 0)
DAY = date(2026, 10, 7)


def chi(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=CHI)


@pytest.fixture(autouse=True)
def _no_standup_nudge():
    """The tick also runs the 6 PM standup nudge: stubbed so these tests never reach Slack or the database."""
    with patch("app.standup.nudge.run_standup_nudge", AsyncMock(return_value="off")):
        yield


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


def test_alert_tick_posts_the_day_before_the_reminders_and_then_refreshes_it():
    # A 05:00-05:30 round is reminded in the same tick: its reminder must thread under the new post.
    order = []
    reminders = AsyncMock(side_effect=lambda now: order.append("reminders") or {"sent": 0, "failed": 0})
    day_post = AsyncMock(side_effect=lambda now: order.append("day_post") or "posted")
    refresh = AsyncMock(side_effect=lambda *a, **k: order.append("refresh") or True)
    with (
        patch("app.interview_day.reminders.run_round_reminders", reminders),
        patch("app.interview_day.post.run_day_post", day_post),
        patch("app.interview_day.post.refresh_today", refresh),
        patch.object(slack_bot, "slack_bot_configured", lambda: False),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert order == ["day_post", "reminders", "refresh"] and out["caller_daily"] == "posted"


def test_alert_tick_refreshes_the_post_unconditionally_once():
    # No interview id: renames, deactivations and archives (which no interview write reports) reach the post too.
    refresh = AsyncMock(return_value=True)
    with (
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 0, "failed": 0})),
        patch("app.interview_day.post.run_day_post", AsyncMock(return_value="already_sent")),
        patch("app.interview_day.post.refresh_today", refresh),
    ):
        asyncio.run(slack_bot.run_alert_tick())
    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs.get("interview_id") is None and not refresh.await_args.args[1:]


def test_alert_tick_survives_a_failing_refresh():
    reminders = AsyncMock(return_value={"sent": 3, "failed": 0})
    with (
        patch("app.interview_day.reminders.run_round_reminders", reminders),
        patch("app.interview_day.post.run_day_post", AsyncMock(return_value="already_sent")),
        patch("app.interview_day.post.refresh_today", AsyncMock(side_effect=RuntimeError("slack down"))),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out == {"caller_reminders": 3, "caller_daily": "already_sent", "after_call_asks": 0, "result_reminders": 0,
                   "standup_nudge": "off"}


def test_alert_tick_sends_no_dms():
    send = AsyncMock(return_value=True)
    with (
        patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 2, "failed": 0})),
        patch("app.interview_day.post.run_day_post", AsyncMock(return_value="posted")),
        patch.object(slack_bot, "slack_bot_configured", lambda: True),
        patch.object(slack_bot, "send_dm", send),
    ):
        out = asyncio.run(slack_bot.run_alert_tick())
    assert out == {"caller_reminders": 2, "caller_daily": "posted", "after_call_asks": 0, "result_reminders": 0,
                   "standup_nudge": "off"}
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
    assert out == {"caller_reminders": 0, "caller_daily": "posted", "after_call_asks": 0, "result_reminders": 0,
                   "standup_nudge": "off"}
    day_post.assert_awaited_once()
