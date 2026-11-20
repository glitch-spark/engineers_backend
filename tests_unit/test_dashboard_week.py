"""GET /dashboard/week: every stored day counts for actuals, plan-based pace, routines, weekly goals (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime
from math import ceil

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, DailyPlan, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.plan_common import Counts, DoneCounts, ItemResult, PlanItem
from app.models.weekly_plan import WeeklyPlan
from app.dashboard_stats import percentile
from app.routers.dashboard import dashboard_week

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

TODAY = "2026-11-18"  # Wednesday; week = Mon 2026-11-16


@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["dashboard-week"],
        document_models=[UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion],
    )


@pytest.fixture
async def me(db):
    user = await UserExtra(name="Ana", email="ana@x.io").insert()
    await WeeklyPlan(
        userId=user.id,
        weekStart=datetime(2026, 11, 16),
        weekNumber=47,
        year=2026,
        startDate=datetime(2026, 11, 16),
        endDate=datetime(2026, 11, 22),
        goal=Counts(bidsSelf=500, bidsBidder=500, interviewsSelf=10),
        routines=[PlanItem(id="r1", text="Review"), PlanItem(id="r2", text="Contacts", kind="count", target=50)],
        weeklyGoals=[PlanItem(id="g1", text="Outreach", kind="count", target=250)],
    ).insert()
    return user


async def day(user, d, logged=False, **kw):
    await DailyPlan(
        userId=user.id,
        date=datetime(2026, 11, d),
        year=2026,
        weekNumber=47,
        loggedAt=datetime(2026, 11, d, 20) if logged else None,
        **kw,
    ).insert()


async def test_counts_unsubmitted_days(me):
    await day(me, 16, logged=True, done=DoneCounts(bidsSelf=100))
    await day(me, 17, done=DoneCounts(bidsBidder=80))
    out = await dashboard_week(me, None, TODAY)
    assert out["bids"]["total"] == 180


async def test_bid_pace_uses_day_targets_and_overrides(me):
    await day(me, 16, logged=True, done=DoneCounts(bidsSelf=100))
    await day(me, 17, targetOverride=Counts(bidsSelf=300, bidsBidder=0))
    out = await dashboard_week(me, None, TODAY)
    assert out["bids"]["pace"]["expected"] == 200 + 300 + 200
    assert out["bids"]["pace"]["perDayNeeded"] == ceil((1000 - out["bids"]["total"]) / 2)


async def test_interview_pace_is_target_times_workdays_over_five(me):
    out = await dashboard_week(me, None, TODAY)
    assert out["interviews"]["pace"]["expected"] == 6  # ceil(10 * 3 / 5)


async def test_weekend_expected_is_full_week(me):
    out = await dashboard_week(me, None, "2026-11-21")  # Saturday
    assert out["bids"]["pace"]["expected"] == 1000 and out["bids"]["pace"]["perDayNeeded"] is None


async def test_routines_and_weekly_goals(me):
    await day(me, 16, results={"r1": ItemResult(done=True), "r2": ItemResult(value=50)})
    await day(me, 17, results={"r1": ItemResult(done=True)})
    out = await dashboard_week(me, None, TODAY)
    assert (out["routines"]["done"], out["routines"]["expected"]) == (3, 6)
    assert out["weeklyGoals"][0]["id"] == "g1" and out["weeklyGoals"][0]["target"] == 250


async def test_percentile_and_streak_stay_logged_only(me):
    await day(me, 17, done=DoneCounts(bidsBidder=80))
    out = await dashboard_week(me, None, TODAY)
    assert out["streak"] == 0


async def test_no_saved_week(db):
    user = await UserExtra(name="Bo", email="bo@x.io").insert()
    out = await dashboard_week(user, None, TODAY)
    assert out["weeklyGoals"] == [] and out["routines"] == {"done": 0, "expected": 0, "rows": []}
    assert out["bids"]["pace"] is None


async def test_bids_percentile_ranks_logged_score_not_drafts(me):
    await day(me, 16, logged=True, done=DoneCounts(bidsBidder=5))
    await day(me, 17, done=DoneCounts(bidsBidder=80))              # draft: in total, not in rank
    for n, bids in (("bo", 10), ("cy", 20)):
        other = await UserExtra(name=n, email=f"{n}@x.io").insert()
        await day(other, 16, logged=True, done=DoneCounts(bidsBidder=bids))
    out = await dashboard_week(me, None, TODAY)
    assert out["bids"]["total"] == 85
    assert out["bids"]["percentile"] == percentile(5, [5, 10, 20])  # last of 3
