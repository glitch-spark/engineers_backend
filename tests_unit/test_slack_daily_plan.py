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


DIVIDER = "━" * 30


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
    rounds = {"self": {"tech_round_1": 1, "intro": 1}, "caller": {"system_design": 1}}
    lines = daily_plan_text(_plan(), _owner(), done=done, rounds=rounds).splitlines()
    assert lines[:-1] == [
        ":spiral_note_pad: <@U123> · *Daily Plan* · Thu 10/1",
        "```",
        DIVIDER,
        "1️⃣ Bids",
        "   Self    : 140 / 150",
        "   Bidder  : 100 / 100",
        "",
        "2️⃣ Interviews",
        "   Self    : 2   Intro(1) · Tech round 1(1)",
        "   Caller  : 1   System Design(1)",
        "",
        "3️⃣ Seek caller",
        "   Status  : ✅ Completed",
        "",
        "4️⃣ Bidder &lt;review&gt;",
        "   Status  : ❌ Not Done",
        "",
        "5️⃣ Extra",
        "   Moved review &amp; prep",
        DIVIDER,
        "```",
    ]
    assert lines[-1].startswith(":link: <")
    assert lines[-1].endswith("/report?week=2026-09-28|Open Report>")


def test_daily_plan_heading():
    owner = SimpleNamespace(name="Pedro", email="p@x.com", slackUserId="U0OWNER001")
    assert daily_plan_text(_plan(), owner).splitlines()[0] == ":spiral_note_pad: <@U0OWNER001> · *Daily Plan* · Thu 10/1"


def test_daily_plan_heading_names_an_unlinked_owner():
    owner = SimpleNamespace(name="Pedro", email="p@x.com", slackUserId=None)
    assert daily_plan_text(_plan(), owner).splitlines()[0] == ":spiral_note_pad: Pedro · *Daily Plan* · Thu 10/1"


def test_daily_plan_text_minimal():
    plan = _plan(goal=Counts(), goalItems=[], done=Counts(), notes="")
    lines = daily_plan_text(plan, _owner(), done=Counts()).splitlines()
    assert lines[1:-1] == [
        "```",
        DIVIDER,
        "1️⃣ Bids",
        "   Self    : 0 / 0",
        "   Bidder  : 0 / 0",
        "",
        "2️⃣ Interviews",
        "   Self    : 0",
        "   Caller  : 0",
        DIVIDER,
        "```",
    ]


def test_daily_plan_text_splits_self_bids_by_region():
    plan = _plan(goal=Counts(bidsSelf=150, bidsSelfUs=80, bidsSelfEu=40, bidsSelfLatam=30, bidsBidder=100))
    done = Counts(bidsSelf=130, bidsSelfUs=70, bidsSelfEu=40, bidsSelfLatam=20, bidsBidder=100)
    lines = daily_plan_text(plan, _owner(), done=done).splitlines()
    assert lines[4:7] == [
        "   Self    : 130 / 150",
        "             US 70/80 · EU 40/40 · LATAM 20/30",
        "   Bidder  : 100 / 100",
    ]


def test_daily_plan_text_other_regions_complete_the_self_total():
    plan = _plan(goal=Counts(bidsSelf=150, bidsSelfUs=80, bidsSelfEu=40, bidsSelfLatam=30, bidsBidder=100))
    done = Counts(bidsSelf=136, bidsSelfUs=70, bidsSelfEu=40, bidsSelfLatam=20, bidsBidder=100)  # 6 Canada / Asia
    lines = daily_plan_text(plan, _owner(), done=done).splitlines()
    assert lines[5] == "             US 70/80 · EU 40/40 · LATAM 20/30 · Other 6"


def test_daily_plan_text_interview_stages_follow_pipeline_order():
    done = Counts(interviewsSelf=7)
    rounds = {"self": {"final": 1, "cultural": 2, "tech_round_2": 1, "live_coding": 1, "intro": 1, "panel": 1}}
    text = daily_plan_text(_plan(), _owner(), done=done, rounds=rounds)
    assert "   Self    : 7   Intro(1) · Tech round 2(1) · Live Coding(1) · Hiring Manager(2) · Panel(1) · Final(1)" in text


def test_daily_plan_text_multiline_note_is_indented():
    lines = daily_plan_text(_plan(notes="Engineer Daily Standup Update\n(Bid details)"), _owner()).splitlines()
    assert lines[-6:-3] == ["5️⃣ Extra", "   Engineer Daily Standup Update", "   (Bid details)"]


def test_daily_plan_text_backticks_cannot_close_the_code_block():
    plan = _plan(goalItems=[ChecklistItem(text="Fix ```x```")], notes="see `y` and ```z```")
    text = daily_plan_text(plan, _owner())
    assert text.count("```") == 2
    assert "`" not in text.replace("```", "")


def test_daily_plan_text_numbers_past_ten():
    items = [ChecklistItem(text=f"Line {n}") for n in range(1, 10)]
    lines = daily_plan_text(_plan(goalItems=items), _owner()).splitlines()
    assert "🔟 Line 8" in lines
    assert "11. Line 9" in lines
    assert "12. Extra" in lines


def test_daily_plan_text_defaults_done_to_stored_done():
    assert "   Self    : 140 / 150" in daily_plan_text(_plan(), _owner()).splitlines()


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
