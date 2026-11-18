"""Unit tests for automatic Goal vs Done values (pure parts)."""

import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_rounds import split_rounds_by_day  # noqa: E402
from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.models import DailyPlan, WeeklyPlan  # noqa: E402
from app.models.plan_common import Counts, DoneCounts, ItemResult, PlanItem  # noqa: E402
from app.plan_auto import (  # noqa: E402
    day_items, day_targets, draft_week, effective_daily_done, goal_progress, weekly_done,
)

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
    return {
        "self": interviews_self,
        "caller": interviews_caller,
        "stages": stages or {"self": {}, "caller": {}},
        "rounds": {"self": {}, "caller": {}},
    }


def _plan(done=None, logged=True):
    return SimpleNamespace(
        done=DoneCounts(**(done or {})),
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
        (date(2026, 9, 29), "intro", False, "intro"),
        (date(2026, 10, 1), "tech", True, "tech_round_1"),
        (date(2026, 10, 2), "final", False, "final"),
    ]


def test_split_rounds_keeps_exact_stage_of_legacy_values():
    iv = _iv("hiring_manager", datetime(2026, 10, 2, 15), history=[("tech2", datetime(2026, 10, 1, 15))])
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "tech", False, "tech_round_2"),
        (date(2026, 10, 2), "hiring", False, "cultural"),
    ]


def test_split_rounds_latest_round_uses_interview_caller():
    iv = _iv("intro", datetime(2026, 10, 1, 15), caller=_caller())
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "intro", True, "intro")
    ]


def test_split_rounds_disabled_caller_is_self():
    iv = _iv("intro", datetime(2026, 10, 1, 15), caller=_caller(enabled=False))
    assert split_rounds_by_day(iv, datetime(2026, 9, 28), datetime(2026, 10, 5), UTC) == [
        (date(2026, 10, 1), "intro", False, "intro")
    ]


def test_split_rounds_ny_saturday_late_round_is_saturday():
    iv = _iv("final", datetime(2026, 10, 4, 3, 30))  # Sat 23:30 in New York
    assert split_rounds_by_day(iv, datetime(2026, 9, 28, 4), datetime(2026, 10, 5, 4), NY) == [
        (date(2026, 10, 3), "final", False, "final")
    ]


def test_split_rounds_skips_excluded_and_canceled_rounds():
    window = (datetime(2026, 9, 28), datetime(2026, 10, 5), UTC)
    excluded = _iv("home_assessment", datetime(2026, 10, 1), history=[("ai_interview", datetime(2026, 9, 30))])
    assert split_rounds_by_day(excluded, *window) == []
    canceled = _iv("intro", datetime(2026, 10, 1), status="canceled")
    assert split_rounds_by_day(canceled, *window) == []
    # A rejected round still happened, so it counts (same as the dashboard tiles).
    rejected = _iv("panel", datetime(2026, 10, 1), status="rejected")
    assert split_rounds_by_day(rejected, *window) == [(date(2026, 10, 1), "panel", False, "panel")]


def test_effective_daily_done_bids_typed_interviews_from_records():
    # Anything typed into interviews is ignored: records are the only source.
    plan = _plan(done={"bidsSelf": 5, "bidsBidder": 4, "interviewsSelf": 9})
    done = effective_daily_done(plan, _day(1, 2))
    assert (done.bidsSelf, done.bidsBidder, done.interviewsSelf, done.interviewsCaller) == (5, 4, 1, 2)


def test_effective_daily_done_without_plan_is_interviews_only():
    done = effective_daily_done(None, _day(2, 1))
    assert done == DoneCounts(interviewsSelf=2, interviewsCaller=1)


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


MON, SAT = date(2026, 10, 5), date(2026, 10, 10)


def _week(monday=MON, uid=None, **kw):
    start = datetime(monday.year, monday.month, monday.day)
    return WeeklyPlan(userId=uid or PydanticObjectId(), weekStart=start, weekNumber=41, year=2026, startDate=start,
                      endDate=start + timedelta(days=6), **kw)


def _day_plan(day=MON, **kw):
    return DailyPlan(userId=PydanticObjectId(), date=datetime(day.year, day.month, day.day), year=2026, weekNumber=41, **kw)


def test_targets_ceil_divide_mon_to_fri():
    w = _week(goal=Counts(bidsSelf=601, interviewsSelf=9))
    assert day_targets(None, w, MON).bidsSelf == 121
    assert day_targets(None, w, MON).interviewsSelf == 0
    assert day_targets(None, w, SAT).bidsSelf == 0


def test_override_wins():
    w = _week(goal=Counts(bidsSelf=100))
    p = _day_plan(targetOverride=Counts(bidsSelf=7))
    assert day_targets(p, w, MON).bidsSelf == 7


def test_no_week_is_zero():
    assert day_targets(None, None, MON) == Counts()


def test_archived_routine_kept_where_it_has_a_result():
    gone = PlanItem(id="a", text="Gym", archived=True)
    w = _week(routines=[gone, PlanItem(id="b", text="Read")])
    assert [i["item"].id for i in day_items(w, None, MON)] == ["b"]
    p = _day_plan(results={"a": ItemResult(done=True)})
    assert [i["item"].id for i in day_items(w, p, MON)] == ["a", "b"]
    assert day_items(w, p, MON)[0]["result"].done


def test_routines_skip_weekend_goals_do_not():
    w = _week(routines=[PlanItem(id="r", text="R")], weeklyGoals=[PlanItem(id="g", text="G")])
    assert [(i["item"].id, i["scope"]) for i in day_items(w, None, SAT)] == [("g", "goal")]


def test_frozen_result_target_wins_over_item():
    w = _week(routines=[PlanItem(id="r", text="Hours", kind="hours", target=2)])
    p = _day_plan(results={"r": ItemResult(value=1, target=3)})
    assert day_items(w, p, MON)[0]["item"].target == 3
    assert w.routines[0].target == 2


def test_goal_progress_sums_counts_and_any_done():
    w = _week(weeklyGoals=[PlanItem(id="g", text="G", kind="count", target=5), PlanItem(id="h", text="H")])
    days = [_day_plan(results={"g": ItemResult(value=2), "h": ItemResult(done=False)}),
            _day_plan(results={"g": ItemResult(value=1.5), "h": ItemResult(done=True)})]
    prog = goal_progress(w, days)
    assert prog["g"] == {"value": 3.5, "done": False, "target": 5}
    assert prog["h"]["done"] is True


@pytest.fixture(autouse=True)
async def db():
    await init_beanie(database=AsyncMongoMockClient()["plan-auto"], document_models=[DailyPlan, WeeklyPlan])


async def test_draft_week_copies_goal_and_routines_not_goals(db):
    uid = PydanticObjectId()
    await _week(MON - timedelta(days=7), uid, goal=Counts(bidsSelf=50),
                routines=[PlanItem(id="r", text="R"), PlanItem(id="x", text="X", archived=True)],
                weeklyGoals=[PlanItem(id="g", text="G")]).insert()
    d = await draft_week(uid, MON)
    assert d.id is None and d.weekStart == datetime(2026, 10, 5)
    assert d.goal.bidsSelf == 50 and [r.id for r in d.routines] == ["r"] and d.weeklyGoals == []


async def test_draft_week_none_without_previous(db):
    assert await draft_week(PydanticObjectId(), MON) is None


async def test_draft_week_uses_app_week_key(db):
    uid, monday = PydanticObjectId(), date(2027, 1, 4)  # ISO week 1 of 2027, the app's week 2
    await _week(monday - timedelta(days=7), uid).insert()
    d = await draft_week(uid, monday)
    assert (d.year, d.weekNumber) == (2027, 2)
