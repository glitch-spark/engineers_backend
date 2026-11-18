"""Plans API with routines and weekly goals: week items, draft weeks, the day's targets / items / progress and the
shared day save (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.weekly_plan import WeeklyPlan
from app.routers import daily_plans
from app.routers.daily_plans import _sync_slack as REAL_SYNC_SLACK, get_day, list_week, put_day
from app.routers.reports import team_report
from app.routers.weekly_plans import get_week, put_week

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
WEEK = "2026-11-09"
MON, TUE, WED = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["daily-plan-api-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(daily_plans, "_today", lambda: date(2026, 11, 13))
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))  # typed bidder bids unless a test moves it
    calls = []

    async def fake_sync(plan, owner, done, rounds):
        calls.append(plan.slackTs)
        if plan.slackTs is None:
            plan.slackChannel, plan.slackTs = "C1", "ts1"
            await plan.save()

    monkeypatch.setattr(daily_plans, "_sync_slack", fake_sync)
    return calls


@pytest.fixture
async def me(db):
    return await UserExtra(name="Ana", email="ana@x.io").insert()


def week_body(**kw):
    body = {
        "goal": {"bidsSelf": 100, "bidsBidder": 50, "interviewsSelf": 5},
        "routines": [{"text": "Bidder review"}, {"text": "Citizen contact", "kind": "count", "target": 50}],
        "weeklyGoals": [{"text": "Email outreach", "kind": "count", "target": 250}],
        "recapNotes": "",
    }
    body.update(kw)
    return body


async def saved_week(me):
    out = await put_week(WEEK, me, week_body())
    review, contact = out["routines"]
    return review["id"], contact["id"], out["weeklyGoals"][0]["id"]


async def stored(me, day):
    return await DailyPlan.find_one({"userId": me.id, "date": datetime(day.year, day.month, day.day)})


async def test_put_week_assigns_ids_and_archives_missing(me):
    review, contact, goal = await saved_week(me)
    assert len({review, contact, goal}) == 3
    out = await put_week(WEEK, me, week_body(
        routines=[{"id": review, "text": "Bidder review"}, {"text": "Interview prep", "kind": "hours", "target": 2}],
        weeklyGoals=[]))
    plan = await WeeklyPlan.find_one({"userId": me.id})
    assert [(r.text, r.archived) for r in plan.routines] == [
        ("Bidder review", False), ("Interview prep", False), ("Citizen contact", True)]
    assert plan.routines[0].id == review and plan.routines[2].id == contact
    assert [(g.id, g.archived) for g in plan.weeklyGoals] == [(goal, True)]
    assert [r["text"] for r in out["routines"]] == ["Bidder review", "Interview prep", "Citizen contact"]
    assert "goalItems" not in out and "dailyGoal" not in out
    with pytest.raises(HTTPException) as err:
        await put_week(WEEK, me, week_body(routines=[{"id": "nope", "text": "X"}]))
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:  # a routine id is not a weekly goal id
        await put_week(WEEK, me, week_body(weeklyGoals=[{"id": review, "text": "X"}]))
    assert err.value.status_code == 422


async def test_get_week_without_plan_returns_draft(me):
    review, contact, goal = await saved_week(me)
    nxt = await get_week("2026-11-16", me, None)
    assert nxt["draft"] is True and nxt["exists"] is False
    assert nxt["goal"]["bidsSelf"] == 100
    assert [r["id"] for r in nxt["routines"]] == [review, contact] and nxt["weeklyGoals"] == []
    assert nxt["days"][0]["goal"]["bidsSelf"] == 20  # targets derived from the draft
    assert await WeeklyPlan.find({"userId": me.id}).count() == 1  # nothing saved
    # The draft's ids are known when the week is saved for the first time.
    saved = await put_week("2026-11-16", me, week_body(routines=nxt["routines"], weeklyGoals=[]))
    assert saved["draft"] is False and saved["exists"] is True
    assert [r["id"] for r in saved["routines"]] == [review, contact]
    blank = await get_week("2026-11-02", me, None)
    assert blank["draft"] is False and blank["exists"] is False and blank["routines"] == []


async def test_get_day_has_targets_items_progress(me):
    review, contact, goal = await saved_week(me)
    await put_day(MON.isoformat(), me, {"results": {goal: {"value": 100}}})
    day = await get_day(TUE.isoformat(), me, None)
    assert (day["targets"]["bidsSelf"], day["targets"]["bidsBidder"], day["targets"]["interviewsSelf"]) == (20, 10, 0)
    assert [(i["id"], i["scope"], i["kind"], i["target"], i["result"]) for i in day["items"]] == [
        (review, "routine", "check", None, None), (contact, "routine", "count", 50, None),
        (goal, "goal", "count", 250, None)]
    assert day["progress"][goal] == {"value": 100, "done": False, "target": 250}
    assert day["submitted"] is False and day["bidsBidderAuto"] == 0
    week = await list_week(me, weekStart=WEEK, userId=None)
    assert week["days"][0]["submitted"] is True and week["days"][1]["targets"]["bidsSelf"] == 20
    assert week["days"][1]["progress"][goal]["value"] == 100 and "bidsBidderAuto" in week["days"][1]


async def test_save_day_freezes_targets(me):
    review, contact, goal = await saved_week(me)
    await put_day(TUE.isoformat(), me, {"results": {contact: {"value": 30}, goal: {"value": 80}, review: {"done": True}}})
    plan = await stored(me, TUE)
    assert (plan.targetOverride.bidsSelf, plan.targetOverride.bidsBidder) == (20, 10)
    assert (plan.results[contact].target, plan.results[goal].target, plan.results[review].target) == (50, 250, None)
    assert plan.submittedAt is not None and plan.loggedAt is not None
    # An override sent with the day wins.
    await put_day(WED.isoformat(), me, {"targetOverride": {"bidsSelf": 7}})
    assert (await stored(me, WED)).targetOverride.bidsSelf == 7


async def test_week_target_edit_keeps_submitted_days(me):
    review, contact, goal = await saved_week(me)
    await put_day(TUE.isoformat(), me, {"results": {contact: {"value": 30}}})
    await put_week(WEEK, me, week_body(
        goal={"bidsSelf": 200, "bidsBidder": 50},
        routines=[{"id": review, "text": "Bidder review"},
                  {"id": contact, "text": "Citizen contact", "kind": "count", "target": 60}],
        weeklyGoals=[{"id": goal, "text": "Email outreach", "kind": "count", "target": 250}]))
    tue, wed = await get_day(TUE.isoformat(), me, None), await get_day(WED.isoformat(), me, None)
    assert tue["targets"]["bidsSelf"] == 20 and wed["targets"]["bidsSelf"] == 40
    assert next(i for i in tue["items"] if i["id"] == contact)["target"] == 50
    assert next(i for i in wed["items"] if i["id"] == contact)["target"] == 60
    # Saving the submitted day again keeps its frozen targets.
    await put_day(TUE.isoformat(), me, {"results": {contact: {"value": 40}}})
    plan = await stored(me, TUE)
    assert plan.targetOverride.bidsSelf == 20 and plan.results[contact].target == 50


async def test_save_day_ignores_unknown_item_ids(me):
    review, contact, goal = await saved_week(me)
    await put_day(TUE.isoformat(), me, {"results": {"nope": {"done": True}, review: {"done": True}},
                                        "focus": ["nope", goal, review]})
    plan = await stored(me, TUE)
    assert set(plan.results) == {review} and plan.focus == [goal]
    with pytest.raises(HTTPException) as err:
        await put_day(TUE.isoformat(), me, {"results": {review: {"value": -1}}})
    assert err.value.status_code == 422


async def test_save_day_sets_submitted_and_posts_once(me, db):
    await saved_week(me)
    await put_day(TUE.isoformat(), me, {"notes": "first"})
    first = await stored(me, TUE)
    out = await put_day(TUE.isoformat(), me, {"notes": "edited"})
    again = await stored(me, TUE)
    assert again.submittedAt == first.submittedAt and again.loggedAt == first.loggedAt
    assert db == [None, "ts1"]  # posted once, then edited in place
    assert out["submitted"] is True and out["notes"] == "edited"
    # Weekend days are accepted as extra work.
    sun = await put_day("2026-11-08", me, {"notes": "extra"})
    assert sun["submitted"] is True and sun["targets"]["bidsSelf"] == 0


async def _bidder_bid(owner, day, hour=10):
    bidder = await Bidder(createdBy=owner.id, name="Bo", rate=0.5, folder="bo", screenshotFolderUrl="s3://s/bo/").insert()
    first = datetime(day.year, day.month, day.day, hour, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    await Bid(kind="bidder", bidderId=bidder.id, ownerId=owner.id, jobKey="bo/1", jobUrl="https://j.io/1",
              firstAt=first, status="approved").insert()


async def test_bidder_done_is_auto_not_typed(me, monkeypatch):
    await _bidder_bid(me, TUE)
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 11, 1))
    out = await put_day(TUE.isoformat(), me, {"done": {"bidsBidder": 99}})
    assert (await stored(me, TUE)).done.bidsBidder == 1
    assert out["bidsBidderAuto"] == 1 and out["done"]["bidsBidder"] == 1
    # Days before the release keep their typed value (also when the body leaves it out).
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))
    await put_day(MON.isoformat(), me, {"done": {"bidsBidder": 7}})
    await put_day(MON.isoformat(), me, {"notes": "edit"})
    assert (await stored(me, MON)).done.bidsBidder == 7


async def test_archived_routine_result_still_returned(me):
    review, contact, goal = await saved_week(me)
    await put_day(MON.isoformat(), me, {"results": {contact: {"value": 50}}})
    await put_week(WEEK, me, week_body(routines=[{"id": review, "text": "Bidder review"}],
                                       weeklyGoals=[{"id": goal, "text": "Email outreach"}]))
    mon, tue = await get_day(MON.isoformat(), me, None), await get_day(TUE.isoformat(), me, None)
    item = next(i for i in mon["items"] if i["id"] == contact)
    assert item["result"]["value"] == 50 and item["target"] == 50
    assert contact not in [i["id"] for i in tue["items"]]


async def test_reports_items_counts(me):
    review, contact, goal = await saved_week(me)
    await put_day(TUE.isoformat(), me, {"results": {
        review: {"done": False}, contact: {"value": 50}, goal: {"value": 10}}})
    await put_day(WED.isoformat(), me, {"results": {review: {"done": True}}})
    row = (await team_report(me, weekStart=WEEK))["users"][0]
    assert (row["itemsDone"], row["itemsTotal"]) == (2, 4)
    assert row["goal"]["bidsSelf"] == 100  # the week's goal, as before
    days = {d["date"]: d for d in (await get_week(WEEK, me, None))["days"]}
    assert (days[TUE.isoformat()]["itemsDone"], days[TUE.isoformat()]["itemsTotal"]) == (1, 3)
    assert days[TUE.isoformat()]["goal"]["bidsSelf"] == 20


async def test_real_sync_slack_builds_the_standup(db, me, monkeypatch):
    """The unpatched _sync_slack loads week data and hands the post its Goal vs Done view."""
    seen = {}

    async def fake_post(plan, owner, **view):
        seen.update(view)
        return "C1", "ts9"

    from app.models.plan_common import Counts

    plan = await DailyPlan(userId=me.id, date=datetime(2026, 11, 12), year=2026, weekNumber=46,
                           submittedAt=datetime(2026, 11, 12, 20)).insert()
    await WeeklyPlan(userId=me.id, weekStart=datetime(2026, 11, 9), startDate=datetime(2026, 11, 9),
                     endDate=datetime(2026, 11, 15), weekNumber=46, year=2026,
                     goal=Counts(bidsSelf=100, interviewsSelf=3, interviewsCaller=2)).insert()
    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)  # the fixture faked it
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    await daily_plans._sync_slack(plan, me, Counts(), {})
    assert seen["targets"].bidsSelf == 20 and seen["week_interviews"] == (0, 5)
    assert (plan.slackChannel, plan.slackTs) == ("C1", "ts9")
