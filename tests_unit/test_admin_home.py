"""Admin Home reads: GET /dashboard/team-today, the day view's `bidsByRegion` / `bidsTotals`, GET /interviews/pipeline
and GET /dashboard/pending (mongomock). docs/superpowers/specs/2026-11-21-admin-home-design.md"""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import itertools
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId, init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder
from app import profile_compare as pc
from app.bidder_alerts import bidder_flag
from app.dashboard_stats import app_tz
from app.models import (
    Account, Application, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication,
    Transaction, UserExtra,
)
from app.models.caller import Caller
from app.models.interview import InterviewStageEvent
from app.models.plan_common import Counts, DoneCounts, ItemResult, PlanItem
from app.models.weekly_plan import WeeklyPlan
from app.routers import dashboard, interviews
from app.routers.daily_plans import get_day
from app.routers.dashboard import dashboard_pending, dashboard_team_today, dashboard_week
from app.routers.interviews import needs_input, pipeline
from app.stats import profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
CHI = ZoneInfo("America/Chicago")
MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
OLD = datetime(2026, 10, 1, 15)  # a profile / bidder old enough for every flag
_n = itertools.count()


def at(day: date, hh=12, mm=0) -> datetime:
    """A wall-clock time of `day` in the app timezone."""
    return datetime.combine(day, time(hh, mm), tzinfo=app_tz())


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat() + "Z"


NOW = at(WED, 15)


def clock(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr(dashboard, "_now", lambda: utc(now))
    monkeypatch.setattr(interviews, "_now", lambda: utc(now))


@pytest.fixture
async def db(monkeypatch):
    profile_loader.clear_cache()
    pc.benchmark_cache.clear()
    await init_beanie(database=AsyncMongoMockClient()["admin-home-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Transaction,
        Application, Caller, JobApplicationLog, ResumeApplication])
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))
    clock(monkeypatch, NOW)
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def boss(db):
    return await user("Boss", "admin")


@pytest.fixture
async def me(db):
    return await user("Ana")


async def user(name, role="staff", **kw) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, **kw).insert()


def rnd(stage="intro", status="scheduled", start=None, **extra) -> dict:
    start = start or at(TUE)
    return InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status, **extra,
    ).model_dump()


async def interview(owner, company, *rounds, account=None) -> Interview:
    return await Interview(
        accountId=account.id if account else PydanticObjectId(), createdBy=owner.id,
        scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds), stage=rounds[-1]["stage"], status=rounds[-1]["status"],
    ).insert()


async def week_plan(owner, **kw) -> None:
    await WeeklyPlan(
        userId=owner.id, weekStart=datetime(2026, 11, 16), weekNumber=47, year=2026,
        startDate=datetime(2026, 11, 16), endDate=datetime(2026, 11, 22), **kw,
    ).insert()


async def day_plan(owner, d: date, **kw) -> None:
    await DailyPlan(userId=owner.id, date=datetime(d.year, d.month, d.day), year=2026, weekNumber=47, **kw).insert()


async def profile(owner, name, region="US", created=OLD, **kw) -> Account:
    return await Account(name=name, region=region, createdBy=owner.id, createdAt=created, **kw).insert()


async def bidder(owner, name, on: Account = None, registered=OLD, created=OLD, archived=False) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5, folder=f"{name.lower()}{next(_n)}", screenshotFolderUrl="s3://s/",
        profileId=on.id if on else None, registeredAt=registered, createdAt=created,
        archivedAt=datetime(2026, 11, 1) if archived else None).insert()


async def bids(b: Bidder, day: date, n=1) -> None:
    first = datetime(day.year, day.month, day.day, 10, tzinfo=CHI).astimezone(UTC).replace(tzinfo=None)
    if n:
        await Bid.get_motor_collection().insert_many([
            Bid(bidderId=b.id, ownerId=b.createdBy, jobKey=f"{b.folder}/{next(_n)}", firstAt=first,
                status="approved").model_dump(by_alias=True, exclude={"id"}, exclude_none=True) for _ in range(n)])


async def team(admin, day=WED) -> list[dict]:
    out = await dashboard_team_today(admin, day.isoformat())
    assert out["day"] == day.isoformat()
    return out["people"]


async def one(admin, who, day=WED) -> dict:
    return next(p for p in await team(admin, day) if p["userId"] == str(who.id))


# --- GET /dashboard/team-today --------------------------------------------------------------------------------


async def test_team_today_is_admin_only(me):
    with pytest.raises(HTTPException) as err:
        await dashboard_team_today(me, WED.isoformat())
    assert err.value.status_code == 403


async def test_team_today_lists_active_non_admin_members(boss, me):
    await user("Gone", isActive=False)
    await user("Root", "admin")
    ben = await user("Ben")
    out = await dashboard_team_today(boss, None)  # no day: today in the app timezone
    assert out["day"] == WED.isoformat()
    assert out["people"] == [
        {"userId": str(me.id), "name": "Ana", "bids": {"done": 0, "target": None}, "todos": {"done": 0, "total": 0},
         "pending": 0, "state": "green"},
        {"userId": str(ben.id), "name": "Ben", "bids": {"done": 0, "target": None}, "todos": {"done": 0, "total": 0},
         "pending": 0, "state": "green"},
    ]


async def test_team_today_bids_and_todos_are_the_days(boss, me):
    review, calls = PlanItem(text="Review"), PlanItem(text="Calls", kind="count", target=5)
    await week_plan(me, goal=Counts(bidsSelf=400, bidsBidder=100), routines=[review, calls],
                    weeklyGoals=[PlanItem(text="Portfolio")])
    await day_plan(me, WED, done=DoneCounts(bidsSelf=60, bidsBidder=20),
                   results={review.id: ItemResult(done=True), calls.id: ItemResult(value=2)})
    await day_plan(me, TUE, done=DoneCounts(bidsSelf=100, bidsBidder=5), targetOverride=Counts(bidsSelf=90),
                   results={review.id: ItemResult(done=True), calls.id: ItemResult(value=5)})
    today, yesterday = await one(boss, me), await one(boss, me, TUE)
    assert (today["bids"], today["todos"], today["state"]) == ({"done": 80, "target": 100}, {"done": 1, "total": 2},
                                                               "amber")
    assert (yesterday["bids"], yesterday["todos"], yesterday["state"]) == (
        {"done": 105, "target": 90}, {"done": 2, "total": 2}, "green")


async def test_team_today_uses_last_weeks_plan_until_this_week_is_saved(boss, me):
    await WeeklyPlan(userId=me.id, weekStart=datetime(2026, 11, 9), weekNumber=46, year=2026,
                     startDate=datetime(2026, 11, 9), endDate=datetime(2026, 11, 15),
                     goal=Counts(bidsSelf=50), routines=[PlanItem(text="Review")]).insert()
    row = await one(boss, me)
    assert (row["bids"], row["todos"]) == ({"done": 0, "target": 10}, {"done": 0, "total": 1})
    assert row["bids"] == (lambda d: {"done": d["done"]["bidsSelf"] + d["done"]["bidsBidder"],
                                      "target": d["targets"]["bidsSelf"] + d["targets"]["bidsBidder"]})(
        await get_day(WED.isoformat(), me, None))


@pytest.mark.parametrize("ended,state", [(16, "red"), (14, "green")])
async def test_an_ended_round_not_marked_is_red(boss, me, monkeypatch, ended, state):
    """Asked how it went OUTCOME_WAIT (15 minutes) after its end."""
    await interview(me, "Acme", rnd(start=at(WED, 10)))
    clock(monkeypatch, at(WED, 11, ended))
    assert (await one(boss, me))["state"] == state


async def test_a_follow_up_due_is_pending_but_not_red(boss, me):
    await interview(me, "Acme", rnd(status="completed", start=at(WED - timedelta(days=9))))
    row = await one(boss, me)
    assert (row["pending"], row["state"]) == (1, "green")


async def test_a_profile_flag_is_pending_but_not_red(boss, me):
    """Nearly everyone has a profile with no interview in two weeks: a flag is counted, it does not colour the dot."""
    await profile(me, "Quiet")  # three weeks without a bid
    row = await one(boss, me)
    assert (row["pending"], row["state"]) == (1, "green")


async def test_a_bidder_flag_is_pending_but_not_red(boss, me):
    bo = await bidder(me, "Bo")
    await bids(bo, WED - timedelta(days=5))
    row = await one(boss, me)
    assert (row["pending"], row["state"]) == (1, "green")


@pytest.mark.parametrize("hh,mm,state", [(11, 59, "green"), (12, 0, "amber"), (17, 0, "amber")])
async def test_bids_under_target_are_amber_from_midday(boss, me, monkeypatch, hh, mm, state):
    await week_plan(me, goal=Counts(bidsSelf=500))
    await day_plan(me, WED, done=DoneCounts(bidsSelf=99))
    clock(monkeypatch, at(WED, hh, mm))
    assert (await one(boss, me))["state"] == state


async def test_yesterdays_bids_under_target_are_amber_whatever_the_time(boss, me, monkeypatch):
    await week_plan(me, goal=Counts(bidsSelf=500))
    await day_plan(me, TUE, done=DoneCounts(bidsSelf=99))
    clock(monkeypatch, at(WED, 8))
    assert (await one(boss, me, TUE))["state"] == "amber"
    assert (await one(boss, me))["state"] == "green"  # today, before midday: bids alone never make it amber


async def test_a_routine_not_done_is_amber_also_before_midday(boss, me, monkeypatch):
    await week_plan(me, routines=[PlanItem(text="Review")])
    clock(monkeypatch, at(WED, 8))
    assert (await one(boss, me))["state"] == "amber"


async def test_all_done_is_green(boss, me):
    review = PlanItem(text="Review")
    await week_plan(me, goal=Counts(bidsSelf=500), routines=[review])
    await day_plan(me, WED, done=DoneCounts(bidsSelf=100), results={review.id: ItemResult(done=True)})
    row = await one(boss, me)
    assert row == {"userId": str(me.id), "name": "Ana", "bids": {"done": 100, "target": 100},
                   "todos": {"done": 1, "total": 1}, "pending": 0, "state": "green"}


async def test_order_is_red_amber_green_then_most_pending_then_name(boss, me):
    zed, bob, cat, dan, eve = [await user(n) for n in ("Zed", "bob", "Cat", "Dan", "Eve")]
    await interview(zed, "Late", rnd(start=at(WED - timedelta(days=1), 10)))   # ended, not marked: red, 1 pending
    await interview(eve, "Late", rnd(start=at(WED - timedelta(days=1), 10)))
    await profile(eve, "Quiet")                                           # red, 2 pending (a flag adds to the count)
    await week_plan(dan, routines=[PlanItem(text="Review")])              # amber, 0 pending
    await week_plan(cat, routines=[PlanItem(text="Review")])
    await interview(cat, "Acme", rnd(status="completed", start=at(WED - timedelta(days=9))))  # amber, 1 pending
    rows = await team(boss)
    assert [(p["name"], p["state"], p["pending"]) for p in rows] == [
        ("Eve", "red", 2), ("Zed", "red", 1), ("Cat", "amber", 1), ("Dan", "amber", 0), ("Ana", "green", 0),
        ("bob", "green", 0)]


async def test_pending_is_the_pending_reads_total(boss, me):
    await interview(me, "Unmarked", rnd())
    await interview(me, "No reason", rnd(status="rejected"))
    await profile(me, "Quiet")
    bo = await bidder(me, "Bo")
    await bids(bo, WED - timedelta(days=5))
    row = await one(boss, me)
    total = (await dashboard_pending(boss, str(me.id)))["counts"]["total"]
    assert row["pending"] == total == 4 and row["state"] == "red"


async def test_team_flags_are_cached_for_a_minute_and_interviews_are_not(boss, me):
    assert (await one(boss, me))["pending"] == 0
    await profile(me, "Quiet")
    await interview(me, "Unmarked", rnd())
    assert (await one(boss, me))["pending"] == 1  # the round at once; the profile flag with the next load
    profile_loader.clear_cache()
    assert (await one(boss, me))["pending"] == 2


# --- GET /daily-plans/{day}: bidsByRegion / bidsTotals --------------------------------------------------------


async def test_bids_by_region_and_totals(me, monkeypatch):
    us, eu, latam = await profile(me, "Usa"), await profile(me, "Eur", "EU"), await profile(me, "Lat", "Latam",
                                                                                           archived=True)
    await week_plan(me, goal=Counts(bidsSelf=150, bidsSelfUs=100, bidsSelfEu=50, bidsBidder=50))
    await day_plan(me, TUE, done=DoneCounts(bidsSelf=25, bidsSelfUs=15, bidsSelfEu=10, bidsBidder=3))
    on_us, on_eu, free = await bidder(me, "Uma", us), await bidder(me, "Eli", eu), await bidder(me, "Fay")
    await bids(on_us, TUE, 2)
    await bids(on_eu, TUE)
    await bids(free, TUE, 4)       # a bidder on no profile: in no region
    await bids(on_us, MON, 9)      # another day
    out = await get_day(TUE.isoformat(), me, None)
    assert out["bidsByRegion"] == [
        {"region": "US", "self": 15, "selfTarget": 20, "bidder": 2, "total": 17},
        {"region": "EU", "self": 10, "selfTarget": 10, "bidder": 1, "total": 11},
    ]  # Latam: an archived profile, no target, no bid
    # before the release day (this file's default) the bidder total is the typed one
    assert out["bidsTotals"] == {"self": 25, "selfTarget": 30, "bidder": 3, "bidderTarget": 10, "total": 28,
                                 "target": 40}
    # from the release day it is the live count the regions are cut from (2 + 1 + 4 on no profile), not the saved 3
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", MON)
    assert (await get_day(TUE.isoformat(), me, None))["bidsTotals"] == {
        "self": 25, "selfTarget": 30, "bidder": 7, "bidderTarget": 10, "total": 32, "target": 40}
    assert latam.region == "Latam"


async def test_a_region_shows_for_a_profile_a_target_or_a_bid_alone(me):
    await profile(me, "Usa")                                                        # a profile
    await day_plan(me, TUE, targetOverride=Counts(bidsSelf=5, bidsSelfEu=5))        # a target
    await bids(await bidder(me, "Lia", await profile(me, "Lat", "Latam", archived=True)), TUE)  # a bid
    out = await get_day(TUE.isoformat(), me, None)
    assert out["bidsByRegion"] == [
        {"region": "US", "self": 0, "selfTarget": None, "bidder": 0, "total": 0},
        {"region": "EU", "self": 0, "selfTarget": 5, "bidder": 0, "total": 0},
        {"region": "Latam", "self": 0, "selfTarget": None, "bidder": 1, "total": 1},
    ]


async def test_bids_totals_without_targets_and_no_regions(me):
    await profile(me, "Can", "Canada")
    await day_plan(me, TUE, done=DoneCounts(bidsSelf=7))
    out = await get_day(TUE.isoformat(), me, None)
    assert out["bidsByRegion"] == []
    assert out["bidsTotals"] == {"self": 7, "selfTarget": None, "bidder": 0, "bidderTarget": None, "total": 7,
                                 "target": None}


# --- GET /interviews/pipeline ---------------------------------------------------------------------------------


async def rows_of(reader, who="") -> list[dict]:
    return (await pipeline(reader, who))["items"]


async def test_pipeline_item_shape(me):
    acct = await profile(me, "Alpha")
    iv = await interview(me, "Acme", rnd(status="passed", start=at(MON)), rnd("tech_round_1", start=at(WED, 18)),
                         account=acct)
    assert await rows_of(me) == [{
        "interviewId": str(iv.id), "companyName": "Acme", "profileName": "Alpha", "stage": "tech_round_1",
        "at": iso(at(WED, 18)), "upcoming": True, "status": "scheduled", "days": None, "followedUpAt": None}]


async def test_scheduled_is_upcoming_until_its_start(me):
    await interview(me, "Ahead", rnd(start=at(WED, 15, 1)))
    await interview(me, "Started", rnd(start=at(WED, 15)))
    await interview(me, "Unmarked", rnd(start=at(MON)))
    assert [(r["companyName"], r["status"], r["upcoming"]) for r in await rows_of(me)] == [
        ("Ahead", "scheduled", True), ("Started", "scheduled", False), ("Unmarked", "scheduled", False)]


async def test_completed_waits_under_seven_days_then_has_no_result(me):
    week = timedelta(days=7)
    await interview(me, "Six", rnd(status="completed", start=NOW - week - timedelta(minutes=59)))  # ended 6d 23h 59m ago
    await interview(me, "Seven", rnd(status="completed", start=NOW - week - timedelta(hours=1)))   # ended 7 days ago
    await interview(me, "Ten", rnd(status="completed", start=NOW - timedelta(days=10, hours=2)))
    assert [(r["companyName"], r["status"], r["days"], r["upcoming"]) for r in await rows_of(me)] == [
        ("Six", "waiting", None, False), ("Seven", "no_result", 7, False), ("Ten", "no_result", 10, False)]


async def test_a_followed_up_round(me):
    chased = [{"at": utc(at(MON, 9))}, {"at": utc(at(TUE, 9))}]
    await interview(me, "Chased", rnd(status="completed", start=NOW - timedelta(days=12), followUps=chased))
    await interview(me, "Early", rnd(status="completed", start=at(TUE), followUps=[{"at": utc(at(TUE, 16))}]))
    assert [(r["companyName"], r["status"], r["days"], r["followedUpAt"]) for r in await rows_of(me)] == [
        ("Early", "followed_up", None, iso(at(TUE, 16))), ("Chased", "followed_up", None, iso(at(TUE, 9)))]


async def test_passed_only_while_no_later_round_exists(me):
    await interview(me, "Passed", rnd(status="passed", start=at(MON)))
    await interview(me, "Next", rnd(status="passed", start=at(MON, 9)), rnd("tech_round_1", "completed", start=at(TUE)))
    assert [(r["companyName"], r["stage"], r["status"]) for r in await rows_of(me)] == [
        ("Next", "tech_round_1", "waiting"), ("Passed", "intro", "passed")]


async def test_closed_interviews_are_out(me):
    await interview(me, "Rejected", rnd(status="rejected", statusReason="pay"))
    await interview(me, "Silent", rnd(status="rejected", statusReason="no_answer"))
    await interview(me, "Canceled", rnd(status="canceled"))
    await interview(me, "Lost later", rnd(status="passed", start=at(MON)), rnd("tech_round_1", "rejected"))
    await interview(me, "Old", rnd(status="completed", start=datetime(2026, 10, 1, 15, tzinfo=UTC)))  # before the data
    assert await rows_of(me) == []


async def test_ai_interviews_and_home_assessments_are_skipped(me):
    await interview(me, "Robot", rnd("ai_interview"))
    await interview(me, "Homework", rnd(status="completed", start=at(MON)), rnd("home_assessment", start=at(WED, 20)))
    assert [(r["companyName"], r["stage"], r["status"]) for r in await rows_of(me)] == [
        ("Homework", "intro", "waiting")]


async def test_pipeline_order_soonest_upcoming_then_most_recent(me):
    await interview(me, "Past old", rnd(status="completed", start=at(MON)))
    await interview(me, "Far", rnd(start=at(WED + timedelta(days=5))))
    await interview(me, "Past new", rnd(status="passed", start=at(TUE)))
    await interview(me, "Soon", rnd(start=at(WED, 16)))
    assert [r["companyName"] for r in await rows_of(me)] == ["Soon", "Far", "Past new", "Past old"]


async def test_pipeline_is_ones_own_and_an_admins_to_read(boss, me):
    ben = await user("Ben")
    await interview(me, "Mine", rnd())
    await interview(ben, "Bens", rnd())
    assert [r["companyName"] for r in await rows_of(me)] == ["Mine"]
    assert [r["companyName"] for r in await rows_of(me, str(me.id))] == ["Mine"]
    assert [r["companyName"] for r in await rows_of(boss, str(ben.id))] == ["Bens"]
    assert await rows_of(boss) == []  # an admin's own
    with pytest.raises(HTTPException) as err:
        await rows_of(me, str(ben.id))
    assert err.value.status_code == 403


# --- GET /dashboard/pending -----------------------------------------------------------------------------------


async def test_pending_interviews_are_the_needs_input_rows(boss, me):
    ben = await user("Ben")
    long_ago = NOW - timedelta(days=9)
    await interview(me, "Unmarked", rnd(start=at(WED, 10)))
    await interview(me, "Chase", rnd(status="completed", start=long_ago))
    await interview(me, "Chased", rnd(status="completed", start=long_ago - timedelta(days=9),
                                     followUps=[{"at": utc(long_ago)}]))
    await interview(me, "Why", rnd(status="canceled", start=at(MON)))
    await interview(me, "Fine", rnd(status="passed", start=at(MON)))
    await interview(ben, "Bens", rnd())
    out = await dashboard_pending(me, None)
    assert [(i["companyName"], i["need"]) for i in out["interviews"]] == [
        ("Unmarked", "outcome"), ("Why", "reason"), ("Chase", "follow_up"), ("Chased", "heard_back")]
    assert out["interviews"] == (await needs_input(me, ""))["items"]
    assert out["interviews"][3]["followedUpAt"] == iso(long_ago)
    assert out["counts"] == {"interviews": 4, "profiles": 0, "bidders": 0, "total": 4}
    assert await dashboard_pending(boss, str(me.id)) == out


async def test_pending_profiles_carry_both_flags_with_their_numbers(me):
    busy, idle = await profile(me, "Busy"), await profile(me, "Idle", created=datetime(2026, 10, 20, 15))
    stale = await profile(me, "Stale")                             # never a bid
    await profile(me, "New", created=datetime(2026, 11, 10, 15))   # too new for either flag
    await profile(me, "Shelved", archived=True)
    on_busy = await bidder(me, "Bo", busy)
    await bids(on_busy, date(2026, 11, 10), 30)
    await day_plan(me, date(2026, 11, 12), done=DoneCounts(bidsSelfByProfile={str(busy.id): 12}),
                   loggedAt=datetime(2026, 11, 12, 20))
    await interview(me, "Acme", rnd(status="passed", start=at(date(2026, 10, 30))), account=busy)
    for day, score in ((10, 60), (11, 70)):
        await ResumeApplication.get_motor_collection().insert_one({
            "accountId": busy.id, "status": "completed", "fixedFrom": None,
            "createdAt": datetime(2026, 11, day, 16), "matchScore": {"total": score}})
    out = await dashboard_pending(me, None)
    assert out["profiles"] == [
        {"accountId": str(busy.id), "name": "Busy", "kind": "no_interviews", "idleDays": 19, "capped": False,
         "bids": 42, "tailorScore": 65.0},
        {"accountId": str(idle.id), "name": "Idle", "kind": "no_bids", "idleDays": 29, "capped": False, "bids": None,
         "tailorScore": None},
        {"accountId": str(stale.id), "name": "Stale", "kind": "no_bids", "idleDays": 48,
         "capped": False, "bids": None, "tailorScore": None},
    ]
    assert out["counts"]["profiles"] == 3


WORKDAYS = [WED - timedelta(days=n) for n in range(1, 25) if (WED - timedelta(days=n)).weekday() < 5]  # newest first


def flag(daily: dict, today=WED, started=OLD.date()):
    return bidder_flag(daily, today, started)


def test_not_bidding_counts_working_days_across_a_weekend():
    fri, thu, mon = date(2026, 11, 13), date(2026, 11, 12), MON
    assert flag({fri: 3}, today=mon) is None                      # the weekend is no working day
    assert flag({fri: 3}, today=TUE) is None                      # Monday alone
    assert flag({fri: 3}) == {"kind": "not_bidding", "days": 2, "recent": None, "before": None}   # Mon, Tue
    assert flag({thu: 3}, today=TUE) == {"kind": "not_bidding", "days": 2, "recent": None, "before": None}
    assert flag({thu: 3, date(2026, 11, 15): 1}, today=TUE) is None   # a Sunday bid: bidding
    assert flag({WED: 5, fri: 3})["days"] == 2                    # today is not counted


def test_not_bidding_needs_a_bid_in_the_fourteen_days_before():
    assert flag({WED - timedelta(days=14): 3})["days"] == 9
    assert flag({WED - timedelta(days=15): 3}) is None
    assert flag({}) is None


def steady(per_day: float, recent: list[int]) -> dict:
    """`recent` on the last 3 working days (oldest first), the 10 before them adding up to 10 x `per_day`."""
    daily = dict(zip(WORKDAYS[2::-1], recent))
    daily.update({d: 0 for d in WORKDAYS[3:13]})
    daily[WORKDAYS[3]] = round(per_day * 10)
    return daily


def test_slowing_is_under_half_of_the_earlier_average():
    assert flag(steady(20, [9, 10, 10])) == {"kind": "slowing", "days": None, "recent": [9, 10, 10], "before": 20.0}
    assert flag(steady(20, [10, 10, 10])) is None                 # exactly half
    assert flag(steady(12.5, [0, 3, 15]))["before"] == 12.5


def test_slowing_needs_an_earlier_average_of_ten_a_day():
    assert flag(steady(10, [4, 5, 5]))["kind"] == "slowing"
    assert flag(steady(9.9, [1, 1, 1])) is None


def test_a_bidder_under_fourteen_days_old_is_never_slowing():
    daily = steady(20, [1, 1, 1])
    assert flag(daily, started=WED - timedelta(days=14))["kind"] == "slowing"
    assert flag(daily, started=WED - timedelta(days=13)) is None
    assert flag({date(2026, 11, 13): 3}, started=WED - timedelta(days=5))["kind"] == "not_bidding"  # still told


def test_not_bidding_wins_over_slowing():
    assert flag(steady(20, [5, 0, 0]))["kind"] == "not_bidding"


async def test_pending_bidders(boss, me):
    ben = await user("Ben")
    quiet, slow, fine = await bidder(me, "Quiet"), await bidder(me, "Slow"), await bidder(me, "Fine")
    gone, invited = await bidder(me, "Gone", archived=True), await bidder(me, "Invited", registered=None)
    fresh = await bidder(me, "Fresh", registered=datetime(2026, 11, 10, 15))
    theirs = await bidder(ben, "Theirs")
    for b in (quiet, gone, invited, theirs):
        await bids(b, date(2026, 11, 13), 2)
    for b in (slow, fresh, fine):
        for d in WORKDAYS[3:13]:
            await bids(b, d, 12)
    for d, n in zip(WORKDAYS[:3], (3, 5, 4)):   # Tue 3, Mon 5, Fri 4
        await bids(slow, d, n)
        await bids(fresh, d, n)
        await bids(fine, d, 6)
    await bids(slow, WED, 40)                   # today is not counted
    out = await dashboard_pending(me, None)
    assert out["bidders"] == [
        {"bidderId": str(quiet.id), "name": "Quiet", "kind": "not_bidding", "days": 2, "recent": None, "before": None},
        {"bidderId": str(slow.id), "name": "Slow", "kind": "slowing", "days": None, "recent": [4, 5, 3], "before": 12.0},
    ]
    assert out["counts"] == {"interviews": 0, "profiles": 0, "bidders": 2, "total": 2}
    assert [b["name"] for b in (await dashboard_pending(boss, str(ben.id)))["bidders"]] == ["Theirs"]


async def test_pending_is_never_another_persons_for_a_non_admin(boss, me):
    ben = await user("Ben")
    await interview(ben, "Bens", rnd())
    await profile(ben, "Quiet")
    await bids(await bidder(ben, "Bo"), date(2026, 11, 13))
    empty = {"interviews": [], "profiles": [], "bidders": [],
             "counts": {"interviews": 0, "profiles": 0, "bidders": 0, "total": 0}}
    assert await dashboard_pending(me, None) == empty
    assert await dashboard_pending(boss, None) == empty  # an admin's own
    for who in (str(ben.id), str(me.id)):  # as the sibling dashboard reads: userId is the admins'
        with pytest.raises(HTTPException) as err:
            await dashboard_pending(me, who)
        assert err.value.status_code == 403
    assert (await dashboard_pending(boss, str(ben.id)))["counts"] == {"interviews": 1, "profiles": 1, "bidders": 1,
                                                                    "total": 3}


# --- GET /dashboard/week: weekOf, bids.week, interviews.byDay -------------------------------------------------

TODAY = WED.isoformat()
LAST_MON = MON - timedelta(days=7)


async def last_week_plan(owner, **kw) -> None:
    start = datetime(LAST_MON.year, LAST_MON.month, LAST_MON.day)
    await WeeklyPlan(userId=owner.id, weekStart=start, weekNumber=46, year=2026, startDate=start,
                     endDate=start + timedelta(days=6), **kw).insert()


async def test_week_of_last_week_reports_that_week_judged_against_today(me):
    await last_week_plan(me, goal=Counts(bidsSelf=500), routines=[PlanItem(id="r1", text="Review")])
    await day_plan(me, LAST_MON, done=DoneCounts(bidsSelf=100), results={"r1": ItemResult(done=True)})
    await day_plan(me, LAST_MON + timedelta(days=1), done=DoneCounts(bidsBidder=40))
    await day_plan(me, MON, done=DoneCounts(bidsSelf=7))
    await interview(me, "Then", rnd(status="passed", start=at(LAST_MON + timedelta(days=1))))
    await interview(me, "Now", rnd(start=at(WED, 20)))
    out = await dashboard_week(me, None, TODAY, "2026-11-11")
    assert out["week"] == {"start": "2026-11-09", "end": "2026-11-14"}
    assert out["bids"]["byDay"] == [
        {"date": "2026-11-09", "done": 100, "self": 100, "bidder": 0, "target": 100}, {"date": "2026-11-10", "done": 40, "self": 0, "bidder": 40, "target": 100},
        {"date": "2026-11-11", "done": 0, "self": 0, "bidder": 0, "target": 100}, {"date": "2026-11-12", "done": 0, "self": 0, "bidder": 0, "target": 100},
        {"date": "2026-11-13", "done": 0, "self": 0, "bidder": 0, "target": 100}]
    assert (out["bids"]["total"], out["bids"]["target"]) == (140, 500)
    [row] = out["routines"]["rows"]
    assert [d["state"] for d in row["days"]] == ["done", "missed", "missed", "missed", "missed"]
    assert (row["doneDays"], row["totalDays"], out["routines"]["done"], out["routines"]["expected"]) == (1, 5, 1, 5)
    assert out["interviews"]["outcomes"] == {
        "held": 1, "passed": 1, "rejected": 0, "noAnswer": 0, "waiting": 0, "canceled": 0, "scheduled": 0}
    assert out["interviews"]["byDay"][1] == {"date": "2026-11-10", "held": 1, "canceled": 0, "scheduled": 0}
    assert out["interviews"]["done"] == 1
    # the pace of a week that is over: everything expected, no day left to catch up
    assert out["bids"]["pace"] == {"expected": 500, "behindBy": 360, "perDayNeeded": None, "onTrack": False}
    assert out["interviews"]["pace"] is None  # no interviews target


async def test_without_week_of_it_is_the_week_of_today(me):
    await week_plan(me, goal=Counts(bidsSelf=500), routines=[PlanItem(id="r1", text="Review")])
    await day_plan(me, MON, done=DoneCounts(bidsSelf=100))
    out = await dashboard_week(me, None, TODAY)
    assert out["week"] == {"start": "2026-11-16", "end": "2026-11-21"}
    assert [d["state"] for d in out["routines"]["rows"][0]["days"]] == ["missed", "missed", "today", "future", "future"]
    assert out["bids"]["pace"] == {"expected": 300, "behindBy": 200, "perDayNeeded": 200, "onTrack": False}
    for same_week in (TODAY, "2026-11-16", "2026-11-22"):  # any date of the week, its Sunday too
        assert await dashboard_week(me, None, TODAY, same_week) == out


@pytest.mark.parametrize("week_of", ["2026-11-23", "2027-01-01", "11/09/2026", "2026-13-01", "last"])
async def test_a_later_week_or_a_malformed_week_of_is_422(me, week_of):
    with pytest.raises(HTTPException) as err:
        await dashboard_week(me, None, TODAY, week_of)
    assert err.value.status_code == 422


async def test_bids_week_sums_the_working_days_and_their_targets(me):
    await week_plan(me, goal=Counts(bidsSelf=500, bidsBidder=100))
    await day_plan(me, MON, done=DoneCounts(bidsSelf=100, bidsBidder=20))
    await day_plan(me, TUE, done=DoneCounts(bidsBidder=80), targetOverride=Counts(bidsSelf=300))
    await day_plan(me, date(2026, 11, 21), done=DoneCounts(bidsSelf=7))  # Saturday: no working day
    bids_out = (await dashboard_week(me, None, TODAY))["bids"]
    assert bids_out["week"] == {"self": 100, "selfTarget": 700, "bidder": 100, "bidderTarget": 80, "total": 200,
                                "target": 780}
    assert bids_out["week"]["total"] == sum(d["done"] for d in bids_out["byDay"])
    assert bids_out["week"]["target"] == sum(d["target"] for d in bids_out["byDay"])
    assert bids_out["total"] == 207  # the existing total keeps the weekend's bids


async def test_bids_week_targets_are_null_when_no_day_has_one(me):
    await week_plan(me, goal=Counts(bidsSelf=500))
    await day_plan(me, MON, done=DoneCounts(bidsSelf=5, bidsBidder=3))
    assert (await dashboard_week(me, None, TODAY))["bids"]["week"] == {
        "self": 5, "selfTarget": 500, "bidder": 3, "bidderTarget": None, "total": 8, "target": 500}
    assert (await dashboard_week(me, None, TODAY, LAST_MON.isoformat()))["bids"]["week"] == {
        "self": 0, "selfTarget": None, "bidder": 0, "bidderTarget": None, "total": 0, "target": None}


async def test_interviews_by_day_follows_the_outcomes_rule(me):
    ben = await user("Ben")
    await interview(me, "Passed", rnd(status="passed", start=at(MON)))
    await interview(me, "Rejected", rnd(status="rejected", start=at(MON), statusReason="pay"))
    await interview(me, "Unmarked", rnd(start=at(MON, 9)))                 # ended, never marked: not held yet
    await interview(me, "Waiting", rnd(status="completed", start=at(TUE)))
    await interview(me, "Canceled", rnd(status="canceled", start=at(TUE)))
    await interview(me, "Tonight", rnd(start=at(WED, 20)))
    await interview(me, "Went on", rnd(status="completed", start=at(TUE, 9)), rnd("tech_round_1", start=at(WED + timedelta(days=1))))
    await interview(me, "Weekend", rnd(start=at(date(2026, 11, 21))))
    await interview(me, "Last week", rnd(status="passed", start=at(LAST_MON)))
    await interview(ben, "Bens", rnd(status="passed", start=at(MON)))
    out = (await dashboard_week(me, None, TODAY))["interviews"]
    assert out["byDay"] == [
        {"date": "2026-11-16", "held": 2, "canceled": 0, "scheduled": 1},
        {"date": "2026-11-17", "held": 2, "canceled": 1, "scheduled": 0},
        {"date": "2026-11-18", "held": 0, "canceled": 0, "scheduled": 1},
        {"date": "2026-11-19", "held": 0, "canceled": 0, "scheduled": 1},
        {"date": "2026-11-20", "held": 0, "canceled": 0, "scheduled": 0},
    ]
    assert out["outcomes"] == {"held": 4, "passed": 2, "rejected": 1, "noAnswer": 0, "waiting": 1, "canceled": 1,
                               "scheduled": 4}
    for key, weekend in (("held", 0), ("canceled", 0), ("scheduled", 1)):  # a weekend round is in outcomes only
        assert sum(d[key] for d in out["byDay"]) + weekend == out["outcomes"][key]
    # the week's rounds, one per working-day round in time order, for the Home week table
    assert [(r["date"], r["companyName"], r["stage"], r["status"]) for r in out["rounds"]] == [
        ("2026-11-16", "Unmarked", "intro", "scheduled"), ("2026-11-16", "Passed", "intro", "passed"),
        ("2026-11-16", "Rejected", "intro", "rejected"), ("2026-11-17", "Went on", "intro", "passed"),
        ("2026-11-17", "Waiting", "intro", "waiting"), ("2026-11-17", "Canceled", "intro", "canceled"),
        ("2026-11-18", "Tonight", "intro", "scheduled"), ("2026-11-19", "Went on", "tech_round_1", "scheduled"),
    ]
    first = out["rounds"][0]
    assert set(first) == {"date", "interviewId", "companyName", "stage", "at", "who", "callerName", "status"}
    assert first["who"] == "self" and first["callerName"] is None and first["at"].endswith("Z")


async def test_week_of_is_ones_own_and_user_id_stays_the_admins(boss, me):
    ben = await user("Ben")
    await day_plan(ben, LAST_MON, done=DoneCounts(bidsSelf=9))
    await day_plan(me, LAST_MON, done=DoneCounts(bidsSelf=4))
    assert (await dashboard_week(me, None, TODAY, LAST_MON.isoformat()))["bids"]["week"]["total"] == 4
    assert (await dashboard_week(boss, str(ben.id), TODAY, LAST_MON.isoformat()))["bids"]["week"]["total"] == 9
    for who in (str(ben.id), str(me.id)):
        with pytest.raises(HTTPException) as err:
            await dashboard_week(me, who, TODAY, LAST_MON.isoformat())
        assert err.value.status_code == 403
