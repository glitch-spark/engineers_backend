"""Unit tests for the daily plan Slack post (text, post, edit in place, delete)."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.config import get_settings
from app.models.plan_common import ChecklistItem, Counts
from app.slack_bot import (
    daily_plan_text,
    delete_daily_plan_message,
    sync_daily_plan_message,
)


def _plan(**kw):
    base = dict(
        id="plan1",
        date=datetime(2026, 10, 1),
        goal=Counts(bidsSelf=150, bidsBidder=100),
        goalItems=[ChecklistItem(text="Seek caller", done=True), ChecklistItem(text="Bidder <review>")],
        done=Counts(bidsSelf=140, bidsBidder=100),
        notes="Moved review & prep",
        slackChannel=None,
        slackTs=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _owner():
    return SimpleNamespace(name="Pedro", email="p@x.com", slackUserId="U123")


def test_daily_plan_text_follow_up():
    done = Counts(bidsSelf=140, bidsBidder=100, interviewsSelf=2, interviewsCaller=1)
    lines = daily_plan_text(_plan(), _owner(), done=done).splitlines()
    assert lines[:-1] == [
        ":spiral_note_pad: *Daily plan* — <@U123>",
        "*10/1 (Thu)*",
        "Bids: self 140/150 · bidder 100/100",
        "Interviews: self 2 · caller 1",
        "✓ Seek caller",
        "○ Bidder &lt;review&gt;",
        "Moved review &amp; prep",
    ]
    assert lines[-1].endswith("/report?week=2026-09-28|Open Report>")


def test_daily_plan_text_minimal():
    plan = _plan(goal=Counts(), goalItems=[], done=Counts(), notes="")
    lines = daily_plan_text(plan, _owner(), done=Counts()).splitlines()
    assert lines[1:-1] == ["*10/1 (Thu)*", "Bids: self 0/0 · bidder 0/0", "Interviews: self 0 · caller 0"]


def test_daily_plan_text_defaults_done_to_stored_done():
    assert "Bids: self 140/150 · bidder 100/100" in daily_plan_text(_plan(), _owner())


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
