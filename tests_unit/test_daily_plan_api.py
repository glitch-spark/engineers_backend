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
from app.models.plan_common import Counts, PlanItem, SatGoal
from app.models.weekly_plan import WeeklyPlan
from app.routers import daily_plans
from app.routers.daily_plans import _sync_slack as REAL_SYNC_SLACK, get_day, list_week, put_day
from app.routers.reports import team_report
from app.routers.weekly_plans import delete_week, get_week, put_week

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
    return await UserExtra(name="Ana", email="ana@x.io", plansOwnWeek=True).insert()


@pytest.fixture
async def planned(db):
    return await UserExtra(name="Pia", email="pia@x.io").insert()  # the admin plans her week


def week_body(**kw):
    body = {
        "goal": {"bidsSelf": 120, "bidsBidder": 60, "interviewsSelf": 5},  # 20 / 10 a day over Mon-Sat
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
    assert nxt["goal"]["bidsSelf"] == 120
    assert [r["id"] for r in nxt["routines"]] == [review, contact] and [g["id"] for g in nxt["weeklyGoals"]] == [goal]
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
    assert day["progress"][goal] == {"value": 100, "done": False, "target": 250, "doneOn": None}
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
        goal={"bidsSelf": 240, "bidsBidder": 60},
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
    assert (row["itemsDone"], row["itemsTotal"]) == (3, 4)  # a touched weekly goal counts as done
    assert row["goal"]["bidsSelf"] == 120  # the week's goal, as before
    days = {d["date"]: d for d in (await get_week(WEEK, me, None))["days"]}
    assert (days[TUE.isoformat()]["itemsDone"], days[TUE.isoformat()]["itemsTotal"]) == (2, 3)
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
                     goal=Counts(bidsSelf=120, interviewsSelf=3, interviewsCaller=2)).insert()
    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)  # the fixture faked it
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    await daily_plans._sync_slack(plan, me, Counts(), {})
    assert seen["targets"].bidsSelf == 20 and seen["week_interviews"] == (0, 5)
    assert (plan.slackChannel, plan.slackTs) == ("C1", "ts9")


async def test_concurrent_first_save_retries_as_update(me, db, monkeypatch):
    """App and Slack both saving a new day: the loser of the insert race hits the unique (userId, date) index and
    applies its save to the winner's document instead of failing."""
    from app import plan_save
    from app.plan_save import DayIn, save_day

    review, contact, _goal = await saved_week(me)
    await put_day(TUE.isoformat(), me, {"results": {review: {"done": True}}, "notes": "app"})
    first = await stored(me, TUE)
    real_find_one, misses = DailyPlan.find_one, []

    def racing_find_one(*args, **kw):  # the first lookup misses the other save's insert
        if not misses:
            misses.append(1)

            async def none():
                return None
            return none()
        return real_find_one(*args, **kw)

    monkeypatch.setattr(plan_save.DailyPlan, "find_one", staticmethod(racing_find_one))
    await save_day(me, TUE, DayIn(results={contact: {"value": 9}}, notes="slack"))
    assert misses == [1] and await DailyPlan.find({"userId": me.id}).count() == 1
    again = await stored(me, TUE)
    assert again.notes == "slack" and again.results[contact].value == 9
    assert again.submittedAt == first.submittedAt and again.slackTs == "ts1"


async def test_concurrent_first_saves_post_once(me, db, monkeypatch):
    """App and Slack saving a new day at once: while the winner's chat.postMessage is in flight, the loser (insert
    race lost, retried as an update) must neither post a second message nor wipe the winner's post id."""
    from app import plan_save
    from app.plan_save import DayIn, save_day

    review, contact, _goal = await saved_week(me)
    posts, edits = [], []
    real_find_one = DailyPlan.find_one

    def missing_once(*args, **kw):  # the loser's lookup misses the winner's insert
        monkeypatch.setattr(plan_save.DailyPlan, "find_one", real_find_one)

        async def none():
            return None
        return none()

    async def fake_post(plan, owner, **view):
        if plan.slackTs:
            edits.append(plan.slackTs)
            return plan.slackChannel, plan.slackTs
        posts.append(plan.notes)
        if len(posts) == 1:  # the loser saves while the winner's post is still in flight
            monkeypatch.setattr(plan_save.DailyPlan, "find_one", staticmethod(missing_once))
            await save_day(me, TUE, DayIn(results={contact: {"value": 9}}, notes="slack"))
        return "C1", f"ts{len(posts)}"

    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    await save_day(me, TUE, DayIn(results={review: {"done": True}}, notes="app"))
    assert posts == ["app"] and await DailyPlan.find({"userId": me.id}).count() == 1
    plan = await stored(me, TUE)
    assert (plan.slackChannel, plan.slackTs) == ("C1", "ts1") and plan.notes == "slack"
    await save_day(me, TUE, DayIn(notes="later"))  # the next save edits the one post
    assert posts == ["app"] and edits == ["ts1"]


async def test_first_post_claim_released_on_failure_and_stale_taken_over(me, db, monkeypatch):
    from app.models.plan_common import Counts

    results = [None, ("C1", "ts7")]
    posts = []

    async def fake_post(plan, owner, **view):
        posts.append(plan.slackTs)
        return results.pop(0)

    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    plan = await DailyPlan(userId=me.id, date=datetime(2026, 11, 12), year=2026, weekNumber=46,
                           submittedAt=datetime(2026, 11, 12, 20)).insert()
    await daily_plans._sync_slack(plan, me, Counts(), {})  # Slack down: the claim is freed
    assert (await DailyPlan.get(plan.id)).slackTs is None
    await DailyPlan.get_motor_collection().update_one({"_id": plan.id}, {"$set": {"slackTs": "posting:1000"}})
    await daily_plans._sync_slack(await DailyPlan.get(plan.id), me, Counts(), {})  # a dead save's claim
    assert posts == [None, None] and (await DailyPlan.get(plan.id)).slackTs == "ts7"
    live = daily_plans._claim_value()
    await DailyPlan.get_motor_collection().update_one({"_id": plan.id}, {"$set": {"slackTs": live}})
    await daily_plans._sync_slack(await DailyPlan.get(plan.id), me, Counts(), {})  # in flight elsewhere: skip
    assert posts == [None, None] and (await DailyPlan.get(plan.id)).slackTs == live


async def _log_week(me):
    out = await put_week(WEEK, me, week_body(routines=[{"text": "Profile review", "kind": "log"}]))
    return out["routines"][0]["id"]


async def _log_result(me, rid):
    item = next(i for i in (await get_day(TUE.isoformat(), me, None))["items"] if i["id"] == rid)
    return item["result"]


async def test_day_saves_log_entries(db, me):
    rid = await _log_week(me)
    pid = str((await Account(createdBy=me.id, name="John", region="US").insert()).id)
    await put_day(TUE.isoformat(), me, {"results": {rid: {"entries": [{"profileId": pid, "note": "rewrote summary"}]}}})
    r = await _log_result(me, rid)
    assert r["done"] is True and r["entries"] == [{"profileId": pid, "note": "rewrote summary"}]


async def test_entries_not_sent_are_kept(db, me):
    rid = await _log_week(me)
    await put_day(TUE.isoformat(), me, {"results": {rid: {"entries": [{"note": "a"}]}}})
    await put_day(TUE.isoformat(), me, {"results": {rid: {}}})
    assert (await _log_result(me, rid))["entries"] == [{"profileId": None, "note": "a"}]
    await put_day(TUE.isoformat(), me, {"results": {rid: {"entries": []}}})
    r = await _log_result(me, rid)
    assert r["entries"] == [] and r["done"] is False


async def test_entries_reject_foreign_profile(db, me):
    rid = await _log_week(me)
    other = await UserExtra(name="Bo", email="bo@x.io").insert()
    pid = str((await Account(createdBy=other.id, name="Eve", region="US").insert()).id)
    with pytest.raises(HTTPException) as e:
        await put_day(TUE.isoformat(), me, {"results": {rid: {"entries": [{"profileId": pid, "note": "x"}]}}})
    assert e.value.status_code == 422 and e.value.detail == "Unknown profile"


async def test_entries_drop_deleted_profile(db, me):
    from bson import ObjectId

    rid = await _log_week(me)
    await put_day(TUE.isoformat(), me,
                  {"results": {rid: {"entries": [{"profileId": str(ObjectId()), "note": "x"}, {"profileId": "junk", "note": "y"}]}}})
    assert (await _log_result(me, rid))["entries"] == [{"profileId": None, "note": "x"}, {"profileId": None, "note": "y"}]


async def test_log_tick_without_entries_is_not_done(db, me):
    rid = await _log_week(me)
    await put_day(TUE.isoformat(), me, {"results": {rid: {"done": True}}})
    assert (await _log_result(me, rid))["done"] is False


async def test_admin_planned_person_cannot_change_week(db, planned):
    with pytest.raises(HTTPException) as e:
        await put_week(WEEK, planned, week_body())
    assert e.value.status_code == 403 and e.value.detail == "Your week is planned by your admin"


async def test_admin_planned_person_can_write_recap(db, planned):
    plan = await WeeklyPlan(userId=planned.id, weekStart=datetime(2026, 11, 9), year=2026, weekNumber=46,
                            startDate=datetime(2026, 11, 9), endDate=datetime(2026, 11, 15),
                            goal=Counts(bidsSelf=600), routines=[PlanItem(text="Review")]).insert()
    out = await put_week(WEEK, planned, week_body(goal={"bidsSelf": 10}, routines=[], recapNotes="good week"))
    got = await WeeklyPlan.get(plan.id)
    assert got.goal.bidsSelf == 600 and got.recapNotes == "good week"
    assert [r.archived for r in got.routines] == [False]
    assert out["recapNotes"] == "good week"


async def test_admin_planned_person_cannot_delete_week(db, planned):
    with pytest.raises(HTTPException) as e:
        await delete_week(WEEK, planned)
    assert e.value.status_code == 403


async def test_get_week_tells_who_plans(db, me, planned):
    assert (await get_week(WEEK, me, None))["plansOwnWeek"] is True
    assert (await get_week(WEEK, planned, None))["plansOwnWeek"] is False
    assert (await get_week(WEEK, me, str(planned.id)))["plansOwnWeek"] is False


async def test_put_week_kind_change_is_remove_plus_add(me):
    review, contact, goal = await saved_week(me)
    await put_week(WEEK, me, week_body(routines=[{"id": review, "text": "Bidder review"},
                                                 {"id": contact, "text": "Citizen contact", "kind": "log"}]))
    plan = await WeeklyPlan.find_one({"userId": me.id})
    assert [(r.id, r.kind, r.archived) for r in plan.routines] == [
        (review, "check", False), (plan.routines[1].id, "log", False), (contact, "count", True)]
    assert plan.routines[1].id not in (review, contact)


async def _admin_week(planned, n=1):
    routines = [PlanItem(id=f"a{i}", text=f"Admin {i}") for i in range(1, n + 1)]
    return await WeeklyPlan(userId=planned.id, weekStart=datetime(2026, 11, 9), year=2026, weekNumber=46,
                            startDate=datetime(2026, 11, 9), endDate=datetime(2026, 11, 15),
                            goal=Counts(bidsSelf=600), routines=routines).insert()


async def test_planned_person_adds_own_item(db, planned):
    await _admin_week(planned)
    out = await put_week(WEEK, planned, week_body(routines=[
        {"id": "a1", "text": "Admin 1"}, {"text": "Discord", "kind": "check", "addedBy": "admin"}]))
    assert [(r["text"], r["addedBy"]) for r in out["routines"]] == [("Admin 1", "admin"), ("Discord", "self")]


async def test_planned_person_cannot_change_admin_items(db, planned):
    await _admin_week(planned)
    await put_week(WEEK, planned, week_body(goal={"bidsSelf": 1}, routines=[{"id": "a1", "text": "Renamed",
                                                                            "kind": "count", "target": 5}]))
    await put_week(WEEK, planned, week_body(goal={"bidsSelf": 1}, routines=[]))
    got = await WeeklyPlan.find_one({"userId": planned.id})
    assert got.goal.bidsSelf == 600
    assert [(r.id, r.text, r.kind, r.archived, r.addedBy) for r in got.routines] == [
        ("a1", "Admin 1", "check", False, "admin")]


async def test_planned_person_edits_and_archives_own(db, planned):
    await _admin_week(planned)
    out = await put_week(WEEK, planned, week_body(routines=[{"id": "a1", "text": "Admin 1"}, {"text": "Mine"}]))
    s1 = out["routines"][1]["id"]
    out = await put_week(WEEK, planned, week_body(routines=[{"id": s1, "text": "Mine v2"}, {"id": "a1", "text": "x"}]))
    assert [(r["text"], r["addedBy"]) for r in out["routines"]] == [("Mine v2", "self"), ("Admin 1", "admin")]
    out = await put_week(WEEK, planned, week_body(routines=[]))
    got = await WeeklyPlan.find_one({"userId": planned.id})
    assert [(r.text, r.archived) for r in got.routines] == [("Admin 1", False), ("Mine v2", True)]


async def test_planned_person_limit_counts_both(db, planned):
    await _admin_week(planned, 15)
    body = week_body(routines=[{"text": f"own {i}"} for i in range(6)])
    with pytest.raises(HTTPException) as e:
        await put_week(WEEK, planned, body)
    assert e.value.status_code == 422 and "at most 20" in e.value.detail
    await put_week(WEEK, planned, week_body(routines=[{"text": f"own {i}"} for i in range(5)]))


async def test_own_week_person_new_items_are_self(db, me):
    out = await put_week(WEEK, me, week_body(routines=[{"text": "Admin-made"}]))
    a = out["routines"][0]
    assert a["addedBy"] == "self"
    out = await put_week(WEEK, me, week_body(routines=[{"id": a["id"], "text": "Edited"}]))
    assert out["routines"][0]["text"] == "Edited"


async def test_own_week_person_renames_admin_item_keeps_admin(db, me):
    await WeeklyPlan(userId=me.id, weekStart=datetime(2026, 11, 9), year=2026, weekNumber=46,
                     startDate=datetime(2026, 11, 9), endDate=datetime(2026, 11, 15),
                     routines=[PlanItem(id="a1", text="Admin 1")]).insert()
    out = await put_week(WEEK, me, week_body(routines=[{"id": "a1", "text": "Mine now"}]))
    assert [(r["text"], r["addedBy"]) for r in out["routines"]] == [("Mine now", "admin")]


async def test_planned_person_weekly_goals_locked_and_archived_admin_stays(db, planned):
    await WeeklyPlan(userId=planned.id, weekStart=datetime(2026, 11, 9), year=2026, weekNumber=46,
                     startDate=datetime(2026, 11, 9), endDate=datetime(2026, 11, 15),
                     routines=[PlanItem(id="old", text="Gone", archived=True)],
                     weeklyGoals=[PlanItem(id="g1", text="Outreach", kind="count", target=9)]).insert()
    out = await put_week(WEEK, planned, week_body(routines=[{"id": "old", "text": "Back"}], weeklyGoals=[
        {"id": "g1", "text": "Changed", "kind": "count", "target": 1}, {"text": "Own goal", "kind": "hours"}]))
    assert [(g["text"], g["target"], g["addedBy"]) for g in out["weeklyGoals"]] == [
        ("Outreach", 9, "admin"), ("Own goal", None, "self")]
    got = await WeeklyPlan.find_one({"userId": planned.id})
    assert [(r.text, r.archived) for r in got.routines] == [("Gone", True)]


async def test_locked_save_without_saved_plan_is_403_and_inserts_nothing(db, planned):
    from app.week_save import save_week
    with pytest.raises(HTTPException) as e:
        await save_week(planned.id, MON, goal=None, routines=[], weekly_goals=[], recap="", added_by="self",
                        locked=True)
    assert e.value.status_code == 403
    assert await WeeklyPlan.find({"userId": planned.id}).to_list() == []


async def test_day_items_return_added_by(db, planned):
    await _admin_week(planned)
    await put_week(WEEK, planned, week_body(routines=[{"id": "a1", "text": "Admin 1"}, {"text": "Mine"}], weeklyGoals=[]))
    day = await get_day(TUE.isoformat(), planned, None)
    assert sorted(i["addedBy"] for i in day["items"]) == ["admin", "self"]


async def _tc_week(me):
    out = await put_week(WEEK, me, week_body(
        routines=[{"text": "Standup note", "kind": "text"},
                  {"text": "Client check", "kind": "choice", "options": ["Done", "Blocked", "N/A"]}], weeklyGoals=[]))
    return out["routines"][0]["id"], out["routines"][1]["id"]


async def _res(me, iid):
    return next(i for i in (await get_day(TUE.isoformat(), me, None))["items"] if i["id"] == iid)


async def test_day_items_carry_options(db, me):
    t, c = await _tc_week(me)
    assert (await _res(me, c))["options"] == ["Done", "Blocked", "N/A"] and (await _res(me, t))["options"] == []


async def test_day_saves_text_keep_clear_and_limit(db, me):
    t, _c = await _tc_week(me)
    await put_day(TUE.isoformat(), me, {"results": {t: {"text": "  set up roles "}}})
    r = (await _res(me, t))["result"]
    assert r["text"] == "set up roles" and r["done"] is True
    await put_day(TUE.isoformat(), me, {"results": {t: {}}})  # not sent: kept
    assert (await _res(me, t))["result"]["text"] == "set up roles"
    await put_day(TUE.isoformat(), me, {"results": {t: {"text": None}}})
    r = (await _res(me, t))["result"]
    assert r["text"] is None and r["done"] is False
    with pytest.raises(HTTPException) as e:
        await put_day(TUE.isoformat(), me, {"results": {t: {"text": "x" * 501}}})
    assert e.value.status_code == 422


async def test_day_saves_choice_and_note(db, me):
    _t, c = await _tc_week(me)
    await put_day(TUE.isoformat(), me, {"results": {c: {"choice": "Blocked", "text": "waiting on client"}}})
    r = (await _res(me, c))["result"]
    assert (r["choice"], r["text"], r["done"]) == ("Blocked", "waiting on client", True)
    await put_day(TUE.isoformat(), me, {"results": {c: {"text": "still waiting"}}})  # choice not sent: kept
    r = (await _res(me, c))["result"]
    assert (r["choice"], r["text"]) == ("Blocked", "still waiting")
    await put_day(TUE.isoformat(), me, {"results": {c: {"choice": ""}}})
    r = (await _res(me, c))["result"]
    assert r["choice"] is None and r["done"] is False


async def test_day_rejects_unknown_option_and_keeps_stale_choice(db, me):
    _t, c = await _tc_week(me)
    with pytest.raises(HTTPException) as e:
        await put_day(TUE.isoformat(), me, {"results": {c: {"choice": "Maybe"}}})
    assert e.value.status_code == 422 and e.value.detail == "Unknown option"
    await put_day(TUE.isoformat(), me, {"results": {c: {"choice": "Blocked"}}})
    await put_week(WEEK, me, week_body(routines=[
        {"id": _t, "text": "Standup note", "kind": "text"},
        {"id": c, "text": "Client check", "kind": "choice", "options": ["Yes", "No"]}], weeklyGoals=[]))
    await put_day(TUE.isoformat(), me, {"results": {c: {"text": "note"}}})  # stored choice no longer an option: kept
    assert (await _res(me, c))["result"]["choice"] == "Blocked"
    with pytest.raises(HTTPException):
        await put_day(TUE.isoformat(), me, {"results": {c: {"choice": "Done"}}})


async def test_progress_save_text_and_choice(db, me):
    t, c = await _tc_week(me)
    await put_day(TUE.isoformat(), me, {"results": {t: {"text": "a"}, c: {"choice": "Done"}}}, submit=False)
    assert (await _res(me, t))["result"]["done"] and (await _res(me, c))["result"]["choice"] == "Done"
    await put_day(TUE.isoformat(), me, {"results": {t: {}, c: {"text": "n"}}}, submit=False)
    assert (await _res(me, t))["result"]["text"] == "a"
    r = (await _res(me, c))["result"]
    assert (r["choice"], r["text"]) == ("Done", "n")
    with pytest.raises(HTTPException) as e:
        await put_day(TUE.isoformat(), me, {"results": {c: {"choice": "Nope"}}}, submit=False)
    assert e.value.detail == "Unknown option"
    await put_day(TUE.isoformat(), me, {"results": {t: {"text": ""}}}, submit=False)
    assert (await _res(me, t))["result"]["done"] is False


async def test_week_get_returns_routine_days(db, me):
    from app.models.plan_common import ItemResult

    r = PlanItem(text="Check inbox")
    await WeeklyPlan(userId=me.id, weekStart=datetime(2026, 11, 9), startDate=datetime(2026, 11, 9),
                     endDate=datetime(2026, 11, 15), weekNumber=46, year=2026, routines=[r]).insert()
    await DailyPlan(userId=me.id, date=datetime(2026, 11, 9), year=2026, weekNumber=46,
                    results={r.id: ItemResult(done=True)}).insert()
    out = await get_week(WEEK, me, None, WED)
    (row,) = out["routineDays"]
    assert row["id"] == r.id
    assert [c["state"] for c in row["days"][:4]] == ["done", "missed", "today", "future"]
    assert (await get_week("2026-11-02", me, None, WED))["routineDays"] == []  # no plan, no draft


async def test_put_week_sat_goal_set_keep_clear(db, me):
    out = await put_week(WEEK, me, week_body(goal={"bidsSelf": 1000, "bidsBidder": 60}, satGoal={"bidsSelf": 100}))
    assert out["satGoal"] == {"bidsSelf": 100, "bidsBidder": None}
    assert [d["goal"]["bidsSelf"] for d in out["days"]] == [180] * 5 + [100]
    assert [d["goal"]["bidsBidder"] for d in out["days"]] == [10] * 6
    assert (await get_week(WEEK, me, None))["satGoal"] == {"bidsSelf": 100, "bidsBidder": None}
    out = await put_week(WEEK, me, week_body(goal={"bidsSelf": 1000}))  # absent (older client): kept
    assert out["satGoal"] == {"bidsSelf": 100, "bidsBidder": None}
    out = await put_week(WEEK, me, week_body(goal={"bidsSelf": 1000}, satGoal=None))
    assert out["satGoal"] is None and [d["goal"]["bidsSelf"] for d in out["days"]] == [167] * 6


async def test_put_week_sat_goal_over_week_total_is_422(db, me):
    with pytest.raises(HTTPException) as e:
        await put_week(WEEK, me, week_body(satGoal={"bidsBidder": 61}))
    assert (e.value.status_code, e.value.detail) == (422, "Saturday bids can't be more than the week's")
    assert await WeeklyPlan.find_one({"userId": me.id}) is None


async def test_planned_person_cannot_change_sat_goal(db, planned):
    plan = await _admin_week(planned)
    plan.satGoal = SatGoal(bidsSelf=50)
    await plan.save()
    for sat in ({"bidsSelf": 0}, None):
        await put_week(WEEK, planned, week_body(routines=[{"id": "a1", "text": "Admin 1", "days": [5]}], satGoal=sat))
        got = await WeeklyPlan.get(plan.id)
        assert got.satGoal == SatGoal(bidsSelf=50) and got.routines[0].days is None  # admin item kept as stored


async def test_put_week_days_kept_and_weekly_goals_forced_none(db, me):
    out = await put_week(WEEK, me, week_body(routines=[{"text": "Sat sweep", "days": [5]}],
                                             weeklyGoals=[{"text": "G", "days": [0]}]))
    assert out["routines"][0]["days"] == [5] and out["weeklyGoals"][0]["days"] is None
