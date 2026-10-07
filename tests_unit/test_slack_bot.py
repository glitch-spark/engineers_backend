"""Unit tests for Slack digest helpers and Know your why."""

import os
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.know_your_why import know_your_why_for_date
from app.models.interview import InterviewStage
from app.slack_bot import (
    _country_flag,
    _fmt_scheduled,
    _interview_blocks,
    _interview_fields,
    _frontend_link,
    _profile_label,
    _stage_label,
    calendar_day_bounds,
    digest_slot_open,
    digest_text,
    interview_dm_eligible,
    notify_interview_dm,
    validate_timezone,
)
from app.slack_timezones import (
    SLACK_DIGEST_TIMEZONES,
    normalize_slack_timezone,
    slack_timezone_catalog,
)


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
    assert len(slack_timezone_catalog()) == 27


def test_catalog_offset_follows_daylight_time():
    summer = slack_timezone_catalog(when=datetime(2026, 7, 15, 16, tzinfo=timezone.utc))
    winter = slack_timezone_catalog(when=datetime(2026, 1, 15, 16, tzinfo=timezone.utc))
    by_summer = {row["value"]: row["label"] for row in summer}
    by_winter = {row["value"]: row["label"] for row in winter}
    assert by_summer["America/New_York"] == "America/New York (GMT−4:00)"
    assert by_winter["America/New_York"] == "America/New York (GMT−5:00)"
    assert by_summer["Europe/Paris"] == "Europe/Paris (GMT+2:00)"
    assert by_winter["Europe/Paris"] == "Europe/Paris (GMT+1:00)"
    assert by_summer["Pacific/Auckland"] == "Pacific/Auckland (GMT+12:00)"
    assert by_winter["Pacific/Auckland"] == "Pacific/Auckland (GMT+13:00)"
    assert by_summer["Asia/Tokyo"] == "Asia/Tokyo (GMT+9:00)"
    assert by_winter["Etc/GMT+12"] == "UTC−12:00"


def test_country_flag():
    assert _country_flag("us") == "🇺🇸"
    assert _country_flag("BR") == "🇧🇷"
    assert _country_flag("") == ""
    assert _country_flag("USA") == ""


def _fake_interview(*, account_id="acc1", position=None, job_url=None, stage=None):
    return SimpleNamespace(
        accountId=account_id,
        scheduledAt=datetime(2026, 8, 29, 12, 0, 0),
        stage=stage or InterviewStage.ai_interview,
        companyName="Acme",
        appliedPosition=position,
        jobUrl=job_url,
    )


def test_profile_label_is_name_only():
    full = SimpleNamespace(name="AI Full-stack", country="br", region="Latam")
    assert _profile_label(full) == "AI Full-stack"
    bare = SimpleNamespace(name="Backend", country=None, region=None)
    assert _profile_label(bare) == "Backend"
    assert _profile_label(None) == ""


def test_fmt_scheduled_mdy_and_abbrev():
    chicago = ZoneInfo("America/Chicago")
    assert _fmt_scheduled(datetime(2026, 9, 23, 12, 0, 0), chicago) == "9/23/26 CDT"
    assert _fmt_scheduled(None, chicago) == "—"


def test_stage_label_humanizes():
    assert _stage_label(InterviewStage.intro) == "Intro"
    assert _stage_label(InterviewStage.tech_round_1) == "Tech Round 1"
    assert _stage_label(InterviewStage.ai_interview) == "AI Interview"
    assert _stage_label(None) == "—"


def test_interview_fields_with_job_url():
    interview = _fake_interview(
        position="Senior Software Engineer",
        job_url="https://jobs.example.com/acme",
        stage=InterviewStage.intro,
    )
    interview.scheduledAt = datetime(2026, 9, 23, 12, 0, 0)
    body = _interview_fields(
        interview,
        profile="Martin Zhang",
        tz=ZoneInfo("America/Chicago"),
    )
    assert body == (
        "*Profile Name:* Martin Zhang\n"
        "*Company Name:* <https://jobs.example.com/acme|Acme>\n"
        "*Applied Position:* Senior Software Engineer "
        "(<https://jobs.example.com/acme|Job description>)\n"
        "*Interview Stage:* Intro\n"
        "*Scheduled Time:* 9/23/26 CDT"
    )


def test_interview_blocks_digest_includes_why_and_link():
    interview = _fake_interview(position="Senior Backend")
    blocks = _interview_blocks(
        "Today's interviews (1)",
        [interview],
        why="Show up curious.",
        profiles={"acc1": "AI Full-stack"},
        tz=ZoneInfo("UTC"),
        include_open_link=True,
    )
    body = blocks[1]["text"]["text"]
    assert "*Profile Name:* AI Full-stack" in body
    assert "*Company Name:* Acme" in body
    assert "*Interview Stage:* AI Interview" in body
    assert any("Open interviews" in str(b) for b in blocks)
    assert any("Know your why" in str(b) for b in blocks)


def test_interview_blocks_instant_omits_why_and_link():
    interview = _fake_interview(account_id="missing")
    blocks = _interview_blocks("New interview", [interview], tz=ZoneInfo("UTC"))
    blob = str(blocks)
    assert "Know your why" not in blob
    assert "Open interviews" not in blob
    assert "*Profile Name:* —" in blocks[1]["text"]["text"]


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
    blob = str(kwargs["blocks"])
    assert "Know your why" not in blob
    assert "Open interviews" not in blob
    assert "*Interview Stage:* AI Interview" in kwargs["blocks"][1]["text"]["text"]


NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 7)


class _FrozenNow(datetime):
    """`datetime` whose now() is Wed Oct 7 2026, 8:00 in the asked zone."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 7, 8, 0, tzinfo=tz)


def _digest_iv(company, *, start=None, stage="intro", job_url=None, account_id="acc1"):
    caller = SimpleNamespace(enabled=True, startsAt=start) if start else None
    return SimpleNamespace(
        accountId=account_id, companyName=company, jobUrl=job_url, stage=stage, caller=caller,
    )


def _digest_pair():
    with_caller = _digest_iv(
        "Acme", start=datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc), job_url="https://jobs.example/1"
    )
    without_caller = _digest_iv("Globex", stage="final", account_id="acc2")
    return with_caller, without_caller


def test_digest_text_rows():
    with_caller, without_caller = _digest_pair()
    text = digest_text(
        TODAY, [with_caller, without_caller],
        profiles={"acc1": "Ian Meron", "acc2": "Maria K"}, tz=NY, why="Ship it",
    )
    assert text.splitlines() == [
        ":alarm_clock: *Today's interviews* — Wed Oct 7 · 2 interviews",
        "• 10:30 AM EDT · Ian Meron · <https://jobs.example/1|Acme> · Intro",
        "• Time TBD · Maria K · Globex · Final",
        f"<{_frontend_link('/interviews')}|Open interviews>",
        "_Know your why: Ship it_",
    ]


def test_digest_text_times_use_the_users_zone():
    chicago = ZoneInfo("America/Chicago")
    iv = _digest_iv("Acme", start=datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc))
    row = digest_text(TODAY, [iv], profiles={}, tz=chicago, why=None).splitlines()[1]
    assert row.startswith("• 9:30 AM CDT · ")


def test_digest_text_without_why_ends_with_the_link():
    with_caller, _ = _digest_pair()
    lines = digest_text(TODAY, [with_caller], profiles={}, tz=NY, why=None).splitlines()
    assert lines[-1] == f"<{_frontend_link('/interviews')}|Open interviews>"
    assert not any("Know your why" in line for line in lines)


def test_digest_text_missing_profile_and_company_show_a_dash():
    iv = _digest_iv(None, account_id=None)
    row = digest_text(TODAY, [iv], profiles={}, tz=NY, why=None).splitlines()[1]
    assert row == "• Time TBD · — · — · Intro"


def test_digest_text_is_one_row_per_interview_in_the_order_given():
    with_caller, without_caller = _digest_pair()
    lines = digest_text(TODAY, [without_caller, with_caller], profiles={}, tz=NY, why=None).splitlines()
    assert lines[1].startswith("• Time TBD") and lines[2].startswith("• 10:30 AM EDT")


def test_digest_text_empty_day():
    assert digest_text(TODAY, [], profiles={}, tz=NY, why=None) == (
        ":alarm_clock: *Today's interviews* — Wed Oct 7 · none today. Have a rest."
    )
    # Nothing else is added on a free day, even with a why.
    assert len(digest_text(TODAY, [], profiles={}, tz=NY, why="Ship it").splitlines()) == 1


def test_digest_text_singular():
    with_caller, _ = _digest_pair()
    first = digest_text(TODAY, [with_caller], profiles={}, tz=NY, why=None).splitlines()[0]
    assert first.endswith("· 1 interview")


@pytest.mark.asyncio
async def test_digest_empty_day_sends_rest_message():
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
    send = AsyncMock(return_value=True)
    find = SimpleNamespace(sort=lambda *_a, **_k: SimpleNamespace(to_list=AsyncMock(return_value=[])))
    with (
        patch("app.slack_bot.digest_slot_open", return_value=True),
        patch("app.slack_bot.datetime", _FrozenNow),
        patch("app.slack_bot._already_sent", AsyncMock(return_value=False)),
        patch("app.slack_bot._mark_sent", mark),
        patch("app.slack_bot.send_dm", send),
        patch("app.slack_bot.Interview.find", return_value=find),
    ):
        sent, reason = await _digest_for_user(user)
    assert sent == 1
    assert reason is None
    send.assert_awaited_once()
    assert send.await_args.kwargs["text"] == (
        ":alarm_clock: *Today's interviews* — Wed Oct 7 · none today. Have a rest."
    )
    assert "blocks" not in send.await_args.kwargs
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_digest_lists_only_the_users_own_interviews():
    from app.slack_bot import _digest_for_user

    user = SimpleNamespace(
        id="user1",
        slackUserId="U123",
        slackAlertsEnabled=True,
        slackTimezone="America/New_York",
        slackDigestHour=8,
        slackDigestMinute=0,
    )
    iv = _fake_interview(job_url="https://jobs.example/1")
    iv.status = None
    iv.caller = None
    find = MagicMock(return_value=SimpleNamespace(
        sort=lambda *_a, **_k: SimpleNamespace(to_list=AsyncMock(return_value=[iv])),
    ))
    send = AsyncMock(return_value=True)
    with (
        patch("app.slack_bot.digest_slot_open", return_value=True),
        patch("app.slack_bot.datetime", _FrozenNow),
        patch("app.slack_bot._already_sent", AsyncMock(return_value=False)),
        patch("app.slack_bot._mark_sent", AsyncMock()),
        patch("app.slack_bot._profile_labels", AsyncMock(return_value={"acc1": "Backend"})),
        patch("app.slack_bot.send_dm", send),
        patch("app.slack_bot.Interview.find", find),
    ):
        sent, _ = await _digest_for_user(user)
    assert sent == 1
    query = find.call_args.args[0]
    assert set(query) == {"createdBy", "scheduledAt"}
    assert query["createdBy"] == "user1"
    assert "blocks" not in send.await_args.kwargs
    lines = send.await_args.kwargs["text"].splitlines()
    assert lines[0] == ":alarm_clock: *Today's interviews* — Wed Oct 7 · 1 interview"
    assert lines[1] == "• Time TBD · Backend · <https://jobs.example/1|Acme> · AI Interview"
    assert lines[-2].endswith("|Open interviews>")
    assert lines[-1].startswith("_Know your why: ") and lines[-1].endswith("_")
    assert "as caller" not in "\n".join(lines)
