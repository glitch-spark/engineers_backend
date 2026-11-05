"""Unit tests must never post to the real Slack workspace, whatever a developer's .env holds (tests_unit/conftest.py
blanks the bot token and channels)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")

from app.config import get_settings
from app.slack_bot import slack_bot_configured


def test_unit_tests_never_reach_slack():
    s = get_settings()
    assert not slack_bot_configured()
    assert not s.SLACK_DAILY_PLAN_CHANNEL_ID
    assert not s.SLACK_TRANSACTIONS_CHANNEL_ID
    assert not s.SLACK_CALLER_CHANNEL_ID
