"""Unit tests for Slack bot helpers (labels, flags, time zones)."""

import os
from datetime import date, datetime, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.models.interview import InterviewStage
from app.slack_bot import (
    _country_flag,
    _profile_label,
    _stage_label,
    validate_timezone,
)
from app.slack_timezones import (
    SLACK_DIGEST_TIMEZONES,
    normalize_slack_timezone,
    slack_timezone_catalog,
)


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


def test_stage_label_humanizes():
    assert _stage_label(InterviewStage.intro) == "Intro"
    assert _stage_label(InterviewStage.tech_round_1) == "Tech Round 1"
    assert _stage_label(InterviewStage.ai_interview) == "AI Interview"
    assert _stage_label(None) == "—"


NY = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 7)
