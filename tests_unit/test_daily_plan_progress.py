"""Draft day save (submit=false) and weekSaved: progress-only saves, saved-week guard and the shared day save (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date
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
from app.routers.daily_plans import get_day, put_day
from app.routers.weekly_plans import put_week

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


@pytest.fixture
async def week(me):
    await put_week(WEEK, me, week_body())
    plan = await WeeklyPlan.find_one({"userId": me.id})
    plan.routines[0].id, plan.routines[1].id, plan.weeklyGoals[0].id = "r1", "r2", "g1"
    await plan.save()


async def test_progress_saves_only_sent_results_without_submitting(db, me, week):
    out = await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.results["r1"].done is True and plan.results["r1"].target is None  # not frozen
    assert plan.submittedAt is None and plan.loggedAt is None and plan.targetOverride is None
    assert db == []                      # _sync_slack never called
    assert out["submitted"] is False and out["weekSaved"] is True


async def test_progress_merges_per_item(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"value": 50}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.results["r1"].done is True and plan.results["r2"].done is True  # 50 >= 50


async def test_progress_keeps_done_and_notes(db, me, week):
    await put_day("2026-11-10", me, {"results": {}, "focus": [], "notes": "hi", "done": {"bidsSelf": 7}})
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.notes == "hi" and plan.done.bidsSelf == 7


async def test_progress_on_submitted_day_edits_post_keeps_submitted_at(db, me, week):
    await put_day("2026-11-10", me, {"results": {}, "focus": [], "notes": "", "done": {}})
    first = (await DailyPlan.find_one({"userId": me.id})).submittedAt
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    assert len(db) == 2 and (await DailyPlan.find_one({"userId": me.id})).submittedAt == first


async def test_progress_unknown_item_is_422(db, me, week):
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-10", me, {"results": {"nope": {"done": True}}}, submit=False)
    assert e.value.status_code == 422


async def test_progress_needs_saved_week(db, me):   # no WeeklyPlan document at all
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-10", me, {"results": {}}, submit=False)
    assert e.value.status_code == 422


async def test_progress_focus_only_when_sent(db, me, week):
    await put_day("2026-11-10", me, {"focus": ["g1"]}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    assert (await DailyPlan.find_one({"userId": me.id})).focus == ["g1"]


async def test_progress_future_day_is_422(db, me, week):
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-20", me, {"results": {}}, submit=False)
    assert e.value.status_code == 422


async def test_carry_over_draft_is_not_a_saved_week(db, me):
    await put_week("2026-11-02", me, week_body())   # last week saved; this week only a carry-over draft
    assert (await get_day("2026-11-10", me, None))["weekSaved"] is False
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-10", me, {"results": {}}, submit=False)
    assert e.value.status_code == 422


async def _r2(me):
    return (await DailyPlan.find_one({"userId": me.id})).results["r2"]


async def test_tick_marks_count_done_without_reaching_target(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True, "value": 31}}}, submit=False)
    r = await _r2(me)
    assert r.done is True and r.value == 31


async def test_untick_count_below_target(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True, "value": 31}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"done": False, "value": 31}}}, submit=False)
    assert (await _r2(me)).done is False


async def test_count_reaching_target_is_done_even_unticked(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"done": False, "value": 50}}}, submit=False)
    assert (await _r2(me)).done is True


async def test_values_only_keeps_stored_done(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True, "value": 31}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"value": 31}}, "focus": [], "notes": "", "done": {}})
    assert (await _r2(me)).done is True


def _days(*specs):
    from types import SimpleNamespace as N
    return [N(date=date(2026, 11, d), results={"g1": N(done=done, value=v)}) for d, v, done in specs]


def test_goal_progress_done_on_and_week_total():
    from types import SimpleNamespace as N
    from app.plan_auto import goal_progress
    weekly = N(weeklyGoals=[N(id="g1", target=250, kind="count")])
    out = goal_progress(weekly, _days((9, 100, False), (10, 150, False)))["g1"]
    assert out["done"] is True and out["doneOn"] is None and out["value"] == 250
    out = goal_progress(weekly, _days((9, 100, False), (10, 150, True)))["g1"]
    assert out["doneOn"] == "2026-11-10"


def test_goal_done_on_is_earliest_done_day():
    from types import SimpleNamespace as N
    from app.plan_auto import goal_progress
    weekly = N(weeklyGoals=[N(id="g1", target=250, kind="count")])
    out = goal_progress(weekly, _days((11, 10, True), (10, 10, True)))["g1"]
    assert out["doneOn"] == "2026-11-10"


async def test_untick_without_value_keeps_stored_value(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True, "value": 31}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"done": False}}}, submit=False)
    r = await _r2(me)
    assert r.done is False and r.value == 31


async def test_slack_values_only_submit_keeps_ticked_count(db, me, week):
    from app.plan_save import DayIn, ItemResultIn, save_day
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True, "value": 31}}}, submit=False)
    await save_day(me, TUE, DayIn(results={"r2": ItemResultIn(value=31)}))
    assert (await _r2(me)).done is True


async def test_value_caused_done_does_not_stick_on_values_only_resubmit(db, me, week):
    from app.plan_save import DayIn, ItemResultIn, save_day
    await save_day(me, TUE, DayIn(results={"r2": ItemResultIn(value=50)}))
    assert (await _r2(me)).done is True
    await save_day(me, TUE, DayIn(results={"r2": ItemResultIn(value=30)}))
    r = await _r2(me)
    assert r.done is False and r.value == 30


async def test_migrated_tick_without_value_stays_done(db, me, week):
    from app.plan_save import DayIn, ItemResultIn, save_day
    await put_day("2026-11-10", me, {"results": {"r2": {"done": True}}}, submit=False)
    await save_day(me, TUE, DayIn(results={"r2": ItemResultIn()}))
    assert (await _r2(me)).done is True


async def test_explicit_null_value_clears_stored_number(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r2": {"value": 31}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"value": None}}}, submit=False)
    assert (await _r2(me)).value is None


async def test_progress_saves_entries(db, me):
    rid = (await put_week(WEEK, me, week_body(routines=[{"text": "Profile review", "kind": "log"}, {"text": "Other"}])))["routines"][0]["id"]
    other = (await WeeklyPlan.find_one({"userId": me.id})).routines[1].id
    await put_day("2026-11-10", me, {"results": {other: {"done": True}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {rid: {"entries": [{"note": "fixed"}]}}}, submit=False)
    res = (await DailyPlan.find_one({"userId": me.id})).results
    assert res[rid].entries[0].note == "fixed" and res[rid].done is True and res[other].done is True
    await put_day("2026-11-10", me, {"results": {rid: {}}}, submit=False)  # not sent: kept
    assert (await DailyPlan.find_one({"userId": me.id})).results[rid].entries[0].note == "fixed"
