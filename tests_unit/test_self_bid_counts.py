"""Self bids from the extension in the Daily Plan (spec 2026-11-15-self-bids-extension-design.md): the pilot shows
tracked next to typed without writing anything; once SELF_BIDS_TRACKED_FROM is set, each (owner, profile) switches
from its first tracked day: the day's self bids = tracked + "other applies" typed, written into the plan (so every
reader of the plan gets them), other profiles stay typed (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import self_bids
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.weekly_plan import WeeklyPlan

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
DAY = date(2026, 11, 17)
_n = itertools.count()


@pytest.fixture
async def db(monkeypatch):
    from app.routers import daily_plans

    await init_beanie(database=AsyncMongoMockClient()["self-bid-counts-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(daily_plans, "_today", lambda: date(2026, 11, 18))
    monkeypatch.setattr(daily_plans, "_sync_slack", _no_slack)
    return monkeypatch


async def _no_slack(*a, **kw):
    return None


def phase2(monkeypatch, day="2026-11-16"):
    monkeypatch.setattr(self_bids, "tracked_from", lambda: date.fromisoformat(day) if day else None)


async def setup():
    pedro = await UserExtra(name="Pedro", email="pedro@x.io").insert()
    alpha = await Account(name="Alpha", createdBy=pedro.id, region="US").insert()
    beta = await Account(name="Beta", createdBy=pedro.id, region="EU").insert()
    return pedro, alpha, beta


async def self_bid(owner, account, day=DAY, hour=10):
    first = datetime(day.year, day.month, day.day, hour, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    await Bid(kind="self", bidderId=None, ownerId=owner.id, accountId=account.id, jobKey=f"self-{owner.id}/x/j{next(_n)}",
              firstAt=first, status="approved", screenshots=[]).insert()


async def put(user, day, by):
    from app.routers.daily_plans import put_day
    return await put_day(day.isoformat(), user, {"done": {"bidsSelfByProfile": {str(k): v for k, v in by.items()}}})


async def get(user, day):
    from app.routers.daily_plans import get_day
    return await get_day(day.isoformat(), user, None)


async def test_pilot_shows_tracked_next_to_typed_and_writes_nothing(db):
    phase2(db, None)
    pedro, alpha, beta = await setup()
    await put(pedro, DAY, {alpha.id: 42, beta.id: 5})
    await self_bid(pedro, alpha)
    await self_bid(pedro, alpha)
    await self_bids.write_through(pedro.id, DAY)          # no-op in the pilot
    out = await get(pedro, DAY)
    assert out["trackedSelfByProfile"] == {str(alpha.id): 2}
    assert out["trackedProfiles"] == []
    plan = await DailyPlan.find_one()
    assert plan.done.bidsSelfByProfile == {str(alpha.id): 42, str(beta.id): 5} and plan.done.bidsSelf == 47


async def test_switch_writes_tracked_for_tracked_profiles_only(db):
    phase2(db)
    pedro, alpha, beta = await setup()
    await put(pedro, DAY, {alpha.id: 42, beta.id: 5})
    await self_bid(pedro, alpha)
    await self_bid(pedro, alpha)
    await self_bids.write_through(pedro.id, DAY)
    plan = await DailyPlan.find_one()
    assert plan.done.bidsSelfByProfile == {str(alpha.id): 2, str(beta.id): 5}
    assert (plan.done.bidsSelf, plan.done.bidsSelfUs, plan.done.bidsSelfEu) == (7, 2, 5)
    out = await get(pedro, DAY)
    assert out["trackedProfiles"] == [str(alpha.id)]


async def test_typed_on_a_tracked_profile_is_other_applies_added_to_tracked(db):
    phase2(db)
    pedro, alpha, beta = await setup()
    await self_bid(pedro, alpha)
    await self_bid(pedro, alpha)
    await put(pedro, DAY, {alpha.id: 3, beta.id: 5})       # 3 applies the extension couldn't capture
    plan = await DailyPlan.find_one()
    assert plan.done.bidsSelfByProfile == {str(alpha.id): 5, str(beta.id): 5}
    assert plan.done.bidsSelfOtherByProfile == {str(alpha.id): 3}
    await self_bid(pedro, alpha)                           # one more tracked: tracked 3 + other 3
    await self_bids.write_through(pedro.id, DAY)
    assert (await DailyPlan.find_one()).done.bidsSelfByProfile[str(alpha.id)] == 6


async def test_a_plan_is_created_when_the_owner_never_filled_one_in(db):
    phase2(db)
    pedro, alpha, _beta = await setup()
    await self_bid(pedro, alpha)
    await self_bids.write_through(pedro.id, DAY)
    plan = await DailyPlan.find_one()
    assert plan is not None and plan.loggedAt is not None and plan.done.bidsSelf == 1


async def test_days_before_the_profiles_first_tracked_day_stay_typed(db):
    phase2(db)
    pedro, alpha, _beta = await setup()
    before = date(2026, 11, 16)
    await put(pedro, before, {alpha.id: 9})
    await self_bid(pedro, alpha)                           # first tracked day: Nov 17
    await self_bids.write_through(pedro.id, before)
    plan = await DailyPlan.find_one({"date": datetime(2026, 11, 16)})
    assert plan.done.bidsSelfByProfile == {str(alpha.id): 9}
    assert (await get(pedro, before))["trackedProfiles"] == []
