"""Unit tests for automatic Goal vs Done values (pure parts)."""

import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_rounds import split_rounds_by_day  # noqa: E402
from app.models.plan_common import Counts  # noqa: E402
from app.plan_auto import effective_daily_done, weekly_done  # noqa: E402

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def _iv(stage, scheduled_at, history=(), status="scheduled", caller=None):
    """history: (stage, scheduledAt) or (stage, scheduledAt, status, caller) tuples."""
    events = []
    for h in history:
        h = tuple(h) + (None, None)[len(h) - 2:] if len(h) < 4 else tuple(h)
        events.append(SimpleNamespace(stage=h[0], scheduledAt=h[1], status=h[2], caller=h[3]))
    return SimpleNamespace(stage=stage, status=status, scheduledAt=scheduled_at, stageHistory=events, caller=caller)


def _caller(enabled=True):
    return SimpleNamespace(enabled=enabled, startsAt=None)


def _day(interviews_self=0, interviews_caller=0, stages=None):
    return {"self": interviews_self, "caller": interviews_caller, "stages": stages or {"self": {}, "caller": {}}}


def _plan(done=None, logged=True):
    return SimpleNamespace(
        done=Counts(**(done or {})),
        loggedAt=datetime(2026, 10, 1, 20) if logged else None,
    )


def test_split_rounds_caller_is_per_round():
    # Intro taken alone; the tech round had a caller (stored on that round).
    iv = _iv(
        "final",
        datetime(2026, 10, 2, 15),
        history=[
            ("intro", datetime(2026, 9, 29, 14), "completed", None),
            ("tech_round_1", datetime(2026, 10, 1, 15), "completed", _caller()),
        ],
    )
    rounds = split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC)
    assert rounds == [
        (date(2026, 9, 29), "intro", False),
        (date(2026, 10, 1), "tech", True),
        (date(2026, 10, 2), "final", False),
    ]


def test_split_rounds_latest_round_uses_interview_caller():
    iv = _iv("intro", datetime(2026, 10, 1, 15), caller=_caller())
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "intro", True)
    ]


def test_split_rounds_disabled_caller_is_self():
    iv = _iv("intro", datetime(2026, 10, 1, 15), caller=_caller(enabled=False))
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "intro", False)
    ]


def test_split_rounds_ny_saturday_late_round_is_saturday():
    iv = _iv("final", datetime(2026, 10, 4, 3, 30))  # Sat 23:30 in New York
    assert split_rounds_by_day(iv, datetime(2026, 9, 28, 4), datetime(2026, 10, 5, 4), NY) == [
        (date(2026, 10, 3), "final", False)
    ]


def test_split_rounds_skips_excluded_and_canceled_rounds():
    window = (datetime(2026, 9, 28), datetime(2026, 10, 5), UTC)
    excluded = _iv("home_assessment", datetime(2026, 10, 1), history=[("ai_interview", datetime(2026, 9, 30))])
    assert split_rounds_by_day(excluded, *window) == []
    canceled = _iv("intro", datetime(2026, 10, 1), status="canceled")
    assert split_rounds_by_day(canceled, *window) == []
    # A rejected round still happened, so it counts (same as the dashboard tiles).
    rejected = _iv("panel", datetime(2026, 10, 1), status="rejected")
    assert split_rounds_by_day(rejected, *window) == [(date(2026, 10, 1), "panel", False)]


def test_effective_daily_done_bids_typed_interviews_from_records():
    # Anything typed into interviews is ignored: records are the only source.
    plan = _plan(done={"bidsSelf": 5, "bidsBidder": 4, "interviewsSelf": 9})
    done = effective_daily_done(plan, _day(1, 2))
    assert (done.bidsSelf, done.bidsBidder, done.interviewsSelf, done.interviewsCaller) == (5, 4, 1, 2)


def test_effective_daily_done_without_plan_is_interviews_only():
    done = effective_daily_done(None, _day(2, 1))
    assert done == Counts(interviewsSelf=2, interviewsCaller=1)


def test_weekly_done_sums_logged_days_bids_and_week_interviews():
    days = [
        (date(2026, 9, 28), Counts(bidsSelf=4, bidsBidder=1), True),
        (date(2026, 9, 30), Counts(bidsSelf=6, bidsBidder=2), True),
        (date(2026, 10, 1), Counts(bidsSelf=7), False),  # goal-only day: not counted
    ]
    done = weekly_done(days, {"self": 3, "caller": 1})
    assert done == Counts(bidsSelf=10, bidsBidder=3, interviewsSelf=3, interviewsCaller=1)


def test_weekly_done_sums_self_regions_and_keeps_pre_split_totals():
    days = [
        (date(2026, 9, 28), Counts(bidsSelf=5), True),  # followed up before the split
        (date(2026, 9, 29), Counts(bidsSelf=6, bidsSelfUs=4, bidsSelfLatam=2), True),
    ]
    done = weekly_done(days, {"self": 0, "caller": 0})
    assert (done.bidsSelf, done.bidsSelfUs, done.bidsSelfEu, done.bidsSelfLatam) == (11, 4, 0, 2)


def test_weekly_done_empty_week():
    assert weekly_done([], {"self": 0, "caller": 0}) == Counts()
