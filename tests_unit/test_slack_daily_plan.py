"""Unit tests for the daily plan Slack post (post, edit in place, delete)."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.config import get_settings
from app.slack_bot import (
    daily_plan_text,
    delete_daily_plan_message,
    sync_daily_plan_message,
)


def _plan(**kw):
    base = dict(
        id="plan1",
        date=datetime(2026, 9, 27),
        today="Ship <filter> & tests",
        tomorrow="",
        slackChannel=None,
        slackTs=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _owner():
    return SimpleNamespace(name="Pedro", email="p@x.com", slackUserId="U123")


def test_daily_plan_text_labels_mention_and_escapes():
    text = daily_plan_text(_plan(), _owner())
    assert "<@U123>" in text
    assert "*9/27(Sun)*" in text
    assert "*9/28(Mon)*" in text
    assert "Ship &lt;filter&gt; &amp; tests" in text
    assert "\n—\n" in text  # empty tomorrow


def test_regular_daily_plan_text_numbers_both_days():
    plan = _plan(
        planType="regular",
        bidsHandsOn=100,
        bidsByBidder=150,
        interviewsDone=2,
        interviewsNew=3,
        todayItems=["Create Linkedin Profile", "Update <resume>"],
        planBidsHandsOn=90,
        planBidsByBidder=160,
        planInterviewsScheduled=5,
        tomorrowItems=["Prep for Acme"],
    )
    lines = daily_plan_text(plan, _owner()).splitlines()
    assert lines[0] == ":spiral_note_pad: *Daily plan* — <@U123>"
    assert lines[1:-1] == [
        "*9/27 (Sun)*",
        "1. Bid",
        "- Bid (hands-on) : 100",
        "- Bid (by bidder) : 150",
        "2. Interview",
        "Done today : 2",
        "New invitations : 3",
        "3. Create Linkedin Profile",
        "4. Update &lt;resume&gt;",
        "*9/28 (Mon)*",
        "1. Bid",
        "- Bid (hands-on) : 90",
        "- Bid (by bidder) : 160",
        "2. Interview",
        "Scheduled : 5",
        "3. Prep for Acme",
    ]
    assert "Open Report" in lines[-1]


def test_regular_daily_plan_text_without_items_or_counts():
    plan = _plan(
        planType="regular",
        bidsHandsOn=0,
        bidsByBidder=0,
        interviewsDone=0,
        interviewsNew=0,
        todayItems=[],
        planBidsHandsOn=0,
        planBidsByBidder=0,
        planInterviewsScheduled=0,
        tomorrowItems=[],
    )
    text = daily_plan_text(plan, _owner())
    assert "Scheduled : 0" in text
    assert not any(line.startswith("3. ") for line in text.splitlines())
    assert "Ship" not in text  # the free-text note belongs to custom plans


@pytest.mark.asyncio
async def test_sync_skips_when_bot_not_configured():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=False),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) is None
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_posts_new_message_to_channel():
    api = AsyncMock(return_value={"ok": True, "channel": "C1", "ts": "111.222"})
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", "C1"),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) == ("C1", "111.222")
    method, body = api.await_args.args
    assert method == "chat.postMessage"
    assert body["channel"] == "C1"


@pytest.mark.asyncio
async def test_sync_skips_new_post_without_channel():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", None),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) is None
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_edits_existing_message_in_place():
    api = AsyncMock(return_value={"ok": True})
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) == ("C1", "111.222")
    api.assert_awaited_once()
    method, body = api.await_args.args
    assert method == "chat.update"
    assert body["channel"] == "C1"
    assert body["ts"] == "111.222"


@pytest.mark.asyncio
async def test_sync_reposts_when_old_message_was_deleted():
    api = AsyncMock(
        side_effect=[
            RuntimeError("message_not_found"),
            {"ok": True, "channel": "C1", "ts": "333.444"},
        ]
    )
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", "C1"),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) == ("C1", "333.444")
    assert [c.args[0] for c in api.await_args_list] == ["chat.update", "chat.postMessage"]


@pytest.mark.asyncio
async def test_sync_never_raises_on_other_errors():
    api = AsyncMock(side_effect=RuntimeError("ratelimited"))
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) is None
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_removes_post():
    api = AsyncMock(return_value={"ok": True})
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        await delete_daily_plan_message(_plan(slackChannel="C1", slackTs="111.222"))
    api.assert_awaited_once_with("chat.delete", {"channel": "C1", "ts": "111.222"})


@pytest.mark.asyncio
async def test_delete_without_post_is_noop():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        await delete_daily_plan_message(_plan())
    api.assert_not_awaited()
