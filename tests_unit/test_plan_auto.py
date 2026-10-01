"""Unit tests for automatic Goal vs Done values (pure parts)."""

import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_rounds import split_rounds_by_day  # noqa: E402
from app.models.plan_common import Counts, DoneOverrides, WeeklyOverrides  # noqa: E402
from app.plan_auto import effective_daily_done, weekly_done  # noqa: E402

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def _iv(stage, scheduled_at, history=(), status="scheduled", caller=None):
    return SimpleNamespace(
        stage=stage,
        status=status,
        scheduledAt=scheduled_at,
        stageHistory=[SimpleNamespace(stage=s, scheduledAt=dt) for s, dt in history],
        caller=caller,
    )


def _caller(starts_at, enabled=True):
    return SimpleNamespace(enabled=enabled, startsAt=starts_at)


def _day(interviews_self=0, interviews_caller=0, stages=None):
    return {"self": interviews_self, "caller": interviews_caller, "stages": stages or {"self": {}, "caller": {}}}


def _plan(done=None, overrides=None, logged=True):
    return SimpleNamespace(
        done=Counts(**(done or {})),
        doneOverrides=DoneOverrides(**(overrides or {})),
        loggedAt=datetime(2026, 10, 1, 20) if logged else None,
    )


def test_split_rounds_caller_same_local_day_only():
    iv = _iv(
        "tech_round_1",
        datetime(2026, 10, 1, 15),
        history=[("intro", datetime(2026, 9, 29, 14))],
        caller=_caller(datetime(2026, 10, 1, 14, 50, tzinfo=timezone.utc)),
    )
    rounds = split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC)
    assert rounds == [(date(2026, 9, 29), "intro", False), (date(2026, 10, 1), "tech", True)]


def test_split_rounds_disabled_caller_is_self():
    iv = _iv("intro", datetime(2026, 10, 1, 15), caller=_caller(datetime(2026, 10, 1, 15), enabled=False))
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "intro", False)
    ]


def test_split_rounds_ny_saturday_late_round_is_saturday():
    iv = _iv("final", datetime(2026, 10, 4, 3, 30))  # Sat 23:30 in New York
    assert split_rounds_by_day(iv, datetime(2026, 9, 28, 4), datetime(2026, 10, 5, 4), NY) == [
        (date(2026, 10, 3), "final", False)
    ]


def test_split_rounds_skips_excluded_and_canceled():
    excluded = _iv("home_assessment", datetime(2026, 10, 1), history=[("ai_interview", datetime(2026, 9, 30))])
    canceled = _iv("rejected", datetime(2026, 10, 1))
    window = (datetime(2026, 9, 28), datetime(2026, 10, 5), UTC)
    assert split_rounds_by_day(excluded, *window) == []
    assert split_rounds_by_day(canceled, *window) == []


def test_effective_daily_done_override_beats_auto():
    plan = _plan(done={"bidsSelf": 5, "bidsBidder": 4, "linkedin": {"US": 2}}, overrides={"interviewsCaller": 3})
    done = effective_daily_done(plan, _day(1, 0), {"US": 4})
    assert (done.bidsSelf, done.bidsBidder, done.linkedin) == (5, 4, {"US": 2})
    assert (done.interviewsSelf, done.interviewsCaller) == (1, 3)


def test_effective_daily_done_profiles_snapshot_when_logged():
    logged = _plan(done={"profiles": {"US": 3}})
    assert effective_daily_done(logged, _day(), {"US": 9}).profiles == {"US": 3}
    not_logged = _plan(logged=False)
    assert effective_daily_done(not_logged, _day(), {"US": 9}).profiles == {"US": 9}
    overridden = _plan(done={"profiles": {"US": 3}}, overrides={"profiles": {"EU": 1}})
    assert effective_daily_done(overridden, _day(), {"US": 9}).profiles == {"EU": 1}


def test_effective_daily_done_without_plan_is_auto_only():
    done = effective_daily_done(None, _day(2, 1), {"EU": 2})
    assert (done.bidsSelf, done.interviewsSelf, done.interviewsCaller, done.profiles) == (0, 2, 1, {"EU": 2})


def test_weekly_done_sums_flows_and_takes_latest_stock():
    days = [
        (date(2026, 9, 28), Counts(bidsSelf=4, bidsBidder=1, profiles={"US": 3}, linkedin={"US": 1}), True),
        (date(2026, 9, 30), Counts(bidsSelf=6, bidsBidder=2, profiles={"US": 4}, linkedin={"US": 2}), True),
        (date(2026, 10, 1), Counts(bidsSelf=0, profiles={"US": 9}), False),  # not logged
    ]
    done = weekly_done(days, {"self": 3, "caller": 1}, WeeklyOverrides())
    assert (done.bidsSelf, done.bidsBidder) == (10, 3)
    assert (done.interviewsSelf, done.interviewsCaller) == (3, 1)
    assert done.profiles == {"US": 4}
    assert done.linkedin == {"US": 2}


def test_weekly_done_override_replaces_field():
    days = [(date(2026, 9, 28), Counts(bidsSelf=4, linkedin={"US": 1}), True)]
    done = weekly_done(days, {"self": 1, "caller": 0}, WeeklyOverrides(bidsSelf=7, linkedin={"EU": 2}))
    assert done.bidsSelf == 7
    assert done.linkedin == {"EU": 2}
    assert done.interviewsSelf == 1


def test_weekly_done_no_logged_days_has_empty_stock():
    done = weekly_done([], {"self": 0, "caller": 0}, WeeklyOverrides())
    assert done == Counts()
