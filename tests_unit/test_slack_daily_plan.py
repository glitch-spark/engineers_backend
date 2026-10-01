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
        goal=Counts(bidsSelf=6, bidsBidder=4, interviewsSelf=1, interviewsCaller=1,
                    profiles={"US": 2, "EU": 1}, linkedin={"US": 2}),
        goalItems=[ChecklistItem(text="Fix resume", done=True), ChecklistItem(text="Call <recruiter>")],
        done=Counts(bidsSelf=5, bidsBidder=4, linkedin={"US": 2}),
        notes="Slow day & quiet",
        slackChannel=None,
        slackTs=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _owner():
    return SimpleNamespace(name="Pedro", email="p@x.com", slackUserId="U123")


def _done():
    return Counts(bidsSelf=5, bidsBidder=4, interviewsSelf=1, interviewsCaller=0,
                  profiles={"US": 2}, linkedin={"US": 2})


def test_daily_plan_text_full():
    tomorrow = _plan(
        date=datetime(2026, 10, 2),
        goal=Counts(bidsSelf=6, bidsBidder=4, interviewsSelf=1, interviewsCaller=1),
        goalItems=[ChecklistItem(text="Prep panel")],
    )
    lines = daily_plan_text(_plan(), _owner(), tomorrow=tomorrow, done=_done()).splitlines()
    assert lines[:-1] == [
        ":spiral_note_pad: *Daily plan* — <@U123>",
        "*10/1 (Thu)*",
        "Bids: self 5/6 · bidder 4/4",
        "Interviews: self 1/1 · caller 0/1",
        "Profiles 2/3 · LinkedIn 2/2",
        "✓ Fix resume",
        "○ Call &lt;recruiter&gt;",
        "Slow day &amp; quiet",
        "*Tomorrow 10/2 (Fri)*: Bids 10 · Interviews 2",
        "○ Prep panel",
    ]
    assert lines[-1].endswith("/report?week=2026-09-28|Open Report>")


def test_daily_plan_text_minimal_without_profiles_items_notes_or_tomorrow():
    plan = _plan(goal=Counts(bidsSelf=3), goalItems=[], done=Counts(), notes="")
    lines = daily_plan_text(plan, _owner(), done=Counts()).splitlines()
    assert lines[1:-1] == [
        "*10/1 (Thu)*",
        "Bids: self 0/3 · bidder 0/0",
        "Interviews: self 0/0 · caller 0/0",
    ]


def test_daily_plan_text_defaults_done_to_stored_done():
    text = daily_plan_text(_plan(), _owner())
    assert "Bids: self 5/6 · bidder 4/4" in text


def test_daily_plan_text_empty_tomorrow_is_omitted():
    empty = _plan(date=datetime(2026, 10, 2), goal=Counts(), goalItems=[])
    assert "Tomorrow" not in daily_plan_text(_plan(), _owner(), tomorrow=empty, done=_done())


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
