"""Unit tests for Slack digest helpers and Know your why."""

import os
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

from app.know_your_why import know_your_why_for_date
from app.models.interview import InterviewStage, InterviewStatus
from app.slack_bot import (
    _country_flag,
    _interview_blocks,
    _profile_label,
    calendar_day_bounds,
    digest_slot_open,
    interview_dm_eligible,
    interview_update_is_noteworthy,
    notify_interview_dm,
    validate_timezone,
)
from app.slack_timezones import SLACK_DIGEST_TIMEZONES, normalize_slack_timezone


def test_digest_slot_open_inside_window():
    now = datetime(2026, 7, 28, 8, 2, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is True


def test_digest_slot_open_outside_window():
    now = datetime(2026, 7, 28, 8, 6, 0)
    assert digest_slot_open(now, hour=8, minute=0, window_minutes=5) is False
    now2 = datetime(2026, 7, 28, 7, 59, 0)
    assert digest_slot_open(now2, hour=8, minute=0, window_minutes=5) is False


def test_calendar_day_bounds():
    start, end = calendar_day_bounds(date(2026, 7, 28))
    assert start == datetime(2026, 7, 28, 0, 0, 0)
    assert end == datetime(2026, 7, 29, 0, 0, 0)


def test_validate_timezone_ok():
    assert validate_timezone("Asia/Hong_Kong") == "Asia/Hong_Kong"
    assert validate_timezone("America/New_York") == "America/New_York"


def test_validate_timezone_invalid():
    try:
        validate_timezone("Asia/Famagusta")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid timezone" in str(exc)


def test_normalize_unknown_falls_back():
    assert normalize_slack_timezone("Asia/Famagusta") == "America/New_York"


def test_catalog_has_27_hourly_offsets():
    assert len(SLACK_DIGEST_TIMEZONES) == 27


def test_country_flag():
    assert _country_flag("us") == "🇺🇸"
    assert _country_flag("BR") == "🇧🇷"
    assert _country_flag("") == ""
    assert _country_flag("USA") == ""


def _fake_interview(*, account_id="acc1", position=None):
    return SimpleNamespace(
        accountId=account_id,
        scheduledAt=datetime(2026, 8, 29, 12, 0, 0),
        stage=InterviewStage.ai_interview,
        companyName="Acme",
        appliedPosition=position,
    )


def test_profile_label_parts():
    full = SimpleNamespace(name="AI Full-stack", country="br", region="Latam")
    assert _profile_label(full) == "🇧🇷 BR · Latam · AI Full-stack"
    bare = SimpleNamespace(name="Backend", country=None, region=None)
    assert _profile_label(bare) == "Backend"
    assert _profile_label(None) == ""


def test_interview_blocks_include_profile_line():
    interview = _fake_interview(position="Senior Backend")
    blocks = _interview_blocks(
        "Today's interviews (1)",
        [interview],
        why="Show up curious.",
        profiles={"acc1": "🇧🇷 BR · Latam · AI Full-stack"},
    )
    body = blocks[1]["text"]["text"]
    assert body == (
        "• *Acme* · Senior Backend\n"
        "  🇧🇷 BR · Latam · AI Full-stack\n"
        "  Sat Aug 29 · `ai_interview`"
    )


def test_interview_blocks_without_profile_match():
    interview = _fake_interview(account_id="missing")
    body = _interview_blocks("t", [interview], why="w")[1]["text"]["text"]
    assert body == "• *Acme*\n  Sat Aug 29 · `ai_interview`"


def test_know_your_why_stable_for_date():
    a = know_your_why_for_date("2026-07-28")
    b = know_your_why_for_date(date(2026, 7, 28))
    assert a == b
    assert isinstance(a, str) and len(a) > 10


def test_interview_dm_eligible():
    assert interview_dm_eligible(None) is False
    assert interview_dm_eligible(SimpleNamespace(slackUserId="", slackAlertsEnabled=True)) is False
    assert interview_dm_eligible(SimpleNamespace(slackUserId="U1", slackAlertsEnabled=False)) is False
    assert interview_dm_eligible(SimpleNamespace(slackUserId="U1", slackAlertsEnabled=True)) is True
    assert interview_dm_eligible(SimpleNamespace(slackUserId="U1", slackAlertsEnabled=None)) is True


def test_interview_update_is_noteworthy_schedule_stage_status_company():
    base = dict(
        prev_scheduled_at=datetime(2026, 9, 24, 12, 0, 0),
        new_scheduled_at=datetime(2026, 9, 24, 12, 0, 0),
        prev_stage=InterviewStage.intro,
        new_stage=InterviewStage.intro,
        prev_status=InterviewStatus.scheduled,
        new_status=InterviewStatus.scheduled,
        prev_company="Acme",
        new_company="Acme",
    )
    assert interview_update_is_noteworthy(**base) is False
    assert interview_update_is_noteworthy(
        **{**base, "new_scheduled_at": datetime(2026, 9, 25, 12, 0, 0)}
    ) is True
    assert interview_update_is_noteworthy(
        **{**base, "new_stage": InterviewStage.tech}
    ) is True
    assert interview_update_is_noteworthy(
        **{**base, "new_status": InterviewStatus.canceled}
    ) is True
    assert interview_update_is_noteworthy(**{**base, "new_company": "Globex"}) is True
    assert interview_update_is_noteworthy(**{**base, "new_company": "Acme "}) is False


@pytest.mark.asyncio
async def test_notify_interview_dm_skips_when_bot_unconfigured():
    interview = _fake_interview()
    interview.createdBy = "user1"
    with patch("app.slack_bot.slack_bot_configured", return_value=False):
        assert await notify_interview_dm(interview, kind="created") is False


@pytest.mark.asyncio
async def test_notify_interview_dm_skips_ineligible_owner():
    interview = _fake_interview()
    interview.createdBy = "user1"
    owner = SimpleNamespace(slackUserId="", slackAlertsEnabled=True)
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot.UserExtra.get", AsyncMock(return_value=owner)),
        patch("app.slack_bot.send_dm", AsyncMock()) as send,
    ):
        assert await notify_interview_dm(interview, kind="created") is False
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_interview_dm_sends_to_owner():
    interview = _fake_interview(position="Senior Backend")
    interview.createdBy = "user1"
    owner = SimpleNamespace(
        slackUserId="U123",
        slackAlertsEnabled=True,
        slackTimezone="America/New_York",
    )
    send = AsyncMock(return_value=True)
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot.UserExtra.get", AsyncMock(return_value=owner)),
        patch("app.slack_bot._profile_labels", AsyncMock(return_value={"acc1": "Backend"})),
        patch("app.slack_bot.send_dm", send),
    ):
        assert await notify_interview_dm(interview, kind="created") is True
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["text"].startswith("New interview:")
    assert kwargs["blocks"][0]["text"]["text"] == "New interview"


@pytest.mark.asyncio
async def test_digest_empty_day_is_not_marked_sent():
    from app.slack_bot import _digest_for_user

    user = SimpleNamespace(
        id="user1",
        slackUserId="U123",
        slackAlertsEnabled=True,
        slackTimezone="America/New_York",
        slackDigestHour=8,
        slackDigestMinute=0,
    )
    mark = AsyncMock()
    find = SimpleNamespace(sort=lambda *_a, **_k: SimpleNamespace(to_list=AsyncMock(return_value=[])))
    with (
        patch("app.slack_bot.digest_slot_open", return_value=True),
        patch("app.slack_bot._already_sent", AsyncMock(return_value=False)),
        patch("app.slack_bot._mark_sent", mark),
        patch("app.slack_bot.Interview.find", return_value=find),
    ):
        sent, reason = await _digest_for_user(user)
    assert sent == 0
    assert reason == "no_interviews"
    mark.assert_not_awaited()
