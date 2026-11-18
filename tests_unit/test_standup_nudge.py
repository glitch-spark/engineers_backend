"""The 6 PM standup nudge: once per weekday at 18:00 Chicago in the daily plan channel."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

import pytest  # noqa: E402
from beanie import init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_bot  # noqa: E402
from app.models import SlackAlertLog  # noqa: E402
from app.standup import nudge  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# Tue 2026-11-17 18:00 Chicago (CST) = 00:00 UTC Wed
TUE_6PM = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def cfg(**over):
    v = dict(SLACK_BOT_TOKEN="x", SLACK_DAILY_PLAN_CHANNEL_ID="C_PLAN", FRONTEND_ORIGIN="https://app.test/")
    v.update(over)
    return SimpleNamespace(**v)


@pytest.fixture
async def slack(monkeypatch):
    database = AsyncMongoMockClient()["standup-nudge-unit"]
    await init_beanie(database=database, document_models=[SlackAlertLog])
    api = AsyncMock(return_value={"ts": "1.1"})
    monkeypatch.setattr(slack_bot, "_slack_api", api)
    monkeypatch.setattr(nudge, "get_settings", lambda: cfg())
    return api


def test_blocks():
    from datetime import date

    text, blocks = nudge.nudge_blocks(date(2026, 11, 17), "https://app.test")
    assert text == "🗒️ Daily standup — Tue 11/17"
    assert "Your draft is ready" in blocks[1]["text"]["text"]
    buttons = blocks[-1]["elements"]
    assert (buttons[0]["action_id"], buttons[0]["value"]) == (nudge.STANDUP_WRITE, "2026-11-17")
    assert buttons[0]["text"]["text"] == "Write my update"
    assert buttons[1]["url"] == "https://app.test/plans?day=2026-11-17"
    assert buttons[1]["text"]["text"] == "Open in app"


async def test_posts_once_at_six_pm_chicago(slack):
    assert await nudge.run_standup_nudge(TUE_6PM) == "posted"
    assert await nudge.run_standup_nudge(TUE_6PM) == "skipped"
    assert slack.await_count == 1
    method, body = slack.await_args.args
    assert method == "chat.postMessage" and body["channel"] == "C_PLAN"
    assert "Time to log today." in str(body["blocks"])


async def test_not_before_six(slack):
    assert await nudge.run_standup_nudge(datetime(2026, 11, 17, 23, 59, tzinfo=timezone.utc)) == "skipped"
    slack.assert_not_awaited()


async def test_not_on_weekends(slack):
    sat = datetime(2026, 11, 22, 0, 30, tzinfo=timezone.utc)  # Sat 18:30 Chicago
    assert await nudge.run_standup_nudge(sat) == "skipped"
    slack.assert_not_awaited()


async def test_off_without_channel_or_token(slack, monkeypatch):
    monkeypatch.setattr(nudge, "get_settings", lambda: cfg(SLACK_DAILY_PLAN_CHANNEL_ID=""))
    assert await nudge.run_standup_nudge(TUE_6PM) == "off"
    monkeypatch.setattr(nudge, "get_settings", lambda: cfg(SLACK_BOT_TOKEN=" "))
    assert await nudge.run_standup_nudge(TUE_6PM) == "off"
    slack.assert_not_awaited()


async def test_failed_post_releases_claim(slack):
    slack.side_effect = [RuntimeError("slack down"), {"ts": "1.2"}]
    assert await nudge.run_standup_nudge(TUE_6PM) == "failed"
    assert await nudge.run_standup_nudge(TUE_6PM) == "posted"


async def test_tick_reports_it():
    with patch("app.interview_day.post.run_day_post", AsyncMock(return_value="already_sent")), \
         patch("app.interview_day.reminders.run_round_reminders", AsyncMock(return_value={"sent": 0, "failed": 0})), \
         patch("app.interview_day.post.refresh_today", AsyncMock(return_value=False)), \
         patch("app.coordination.chase.run_coordination_chase", AsyncMock(return_value={})), \
         patch("app.standup.nudge.run_standup_nudge", AsyncMock(return_value="posted")):
        out = await slack_bot.run_alert_tick()
    assert out["standup_nudge"] == "posted"
