"""scripts/migrate_plans_routines.py: parse lines, split week lines, map day lines (mongomock only)."""

import os
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.models import DailyPlan, WeeklyPlan  # noqa: E402
from app.plan_auto import day_targets  # noqa: E402
from scripts.migrate_plans_routines import migrate, parse_item  # noqa: E402

USER = PydanticObjectId()
MON = datetime(2026, 11, 9)
TUE = datetime(2026, 11, 10)


@pytest.mark.parametrize("text,kind,target", [
    ("Send 50 emails to citizens in db", "count", 50),
    ("Email outreach x 50", "count", 50),
    ("Email outreach x 250", "count", 250),
    ("Email outreach ×250", "count", 250),
    ("Send Contact to 250 Citizens.", "count", 250),
    ("Interview Prep: 2h", "hours", 2),
    ("Bidder Check: 30min", "hours", 0.5),
    ("Bidder Check: 1h", "hours", 1),
    ("Bidder Check: 1.5h", "hours", 1.5),
    ("Citizen Search on Linkedin and other job posting platforms: 2h/daily", "hours", 2),
    ("Bidder Management", "check", None),
    ("Newsela Work", "check", None),
    ("Real Man Search: Telegram(...)&Discord(...): 20 connections/day, By broker", "count", 20),
])
def test_parse_item_examples(text, kind, target):
    item = parse_item(text)
    assert (item.text, item.kind, item.target) == (text, kind, target)


async def _db():
    db = AsyncMongoMockClient()["migrate-routines-unit"]
    await init_beanie(database=db, document_models=[DailyPlan, WeeklyPlan])
    return db


def _week(**kw):
    return {"userId": USER, "weekStart": MON, "weekNumber": 46, "year": 2026, "startDate": MON,
            "endDate": datetime(2026, 11, 15), "goal": {"bidsSelf": 50}, **kw}


def _day(**kw):
    return {"userId": USER, "date": TUE, "year": 2026, "weekNumber": 46, **kw}


async def _setup(db, week=None, day=None):
    await db["weeklyplans"].insert_one(week or _week(
        dailyGoalItems=[{"text": "Bidder Management", "done": False}, {"text": "Email outreach x 50", "done": False}],
        goalItems=[{"text": "bidder  management.", "done": False}, {"text": "Ship the thing", "done": False}]))
    await db["dailyplans"].insert_one(day or _day(
        goal={"bidsSelf": 12}, loggedAt=datetime(2026, 11, 10, 22),
        goalItems=[{"text": "Bidder management", "done": True}, {"text": "email outreach x 50", "done": False},
                   {"text": "Extra thing: 2h", "done": True}]))


async def test_week_lines_split_into_routines_and_goals():
    db = await _db()
    await _setup(db)
    await migrate(True, db)
    week = await db["weeklyplans"].find_one({})
    assert [(r["text"], r["kind"], r["target"]) for r in week["routines"]] == [
        ("Bidder Management", "check", None), ("Email outreach x 50", "count", 50)]
    # the matching goal line is a routine, not a goal; unmatched ones stay goals
    assert [g["text"] for g in week["weeklyGoals"]][0] == "Ship the thing"


async def test_day_lines_map_to_results():
    db = await _db()
    await _setup(db)
    await migrate(True, db)
    week, day = await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})
    r = {x["text"]: x["id"] for x in week["routines"]}
    assert day["results"][r["Bidder Management"]]["done"] is True
    assert day["results"][r["Email outreach x 50"]]["done"] is False
    assert day["submittedAt"] == datetime(2026, 11, 10, 22)


async def test_unmatched_day_line_becomes_goal():
    db = await _db()
    await _setup(db)
    await migrate(True, db)
    week, day = await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})
    goal = next(g for g in week["weeklyGoals"] if g["text"] == "Extra thing: 2h")
    assert (goal["kind"], goal["target"]) == ("hours", 2)
    assert day["results"][goal["id"]]["done"] is True


async def test_existing_day_numbers_unchanged():
    db = await _db()
    await _setup(db)
    await migrate(True, db)
    plan = DailyPlan.model_validate(await db["dailyplans"].find_one({}))
    weekly = WeeklyPlan.model_validate(await db["weeklyplans"].find_one({}))
    assert day_targets(plan, weekly, TUE.date()).bidsSelf == 12  # the day's stored goal, not 50 / 5
    # a day without bids keeps the derived numbers
    await db["dailyplans"].insert_one(_day(date=datetime(2026, 11, 11), goalItems=[]))
    await migrate(True, db)
    plain = DailyPlan.model_validate(await db["dailyplans"].find_one({"date": datetime(2026, 11, 11)}))
    assert plain.targetOverride is None
    assert day_targets(plain, weekly, datetime(2026, 11, 11).date()).bidsSelf == 10


async def test_idempotent_and_dry_run_writes_nothing():
    db = await _db()
    await _setup(db)
    before = [await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})]
    await migrate(False, db)
    assert before == [await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})]
    await migrate(True, db)
    after = [await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})]
    counts = await migrate(True, db)
    assert after == [await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})]
    assert counts["weeklyMigrated"] == counts["dailyMigrated"] == 0


async def test_day_without_week_creates_week_and_keeps_numbers():
    db = await _db()
    wed = datetime(2026, 11, 11)
    await db["dailyplans"].insert_many([
        _day(goal={"bidsSelf": 12}, loggedAt=datetime(2026, 11, 10, 22),
             goalItems=[{"text": "Bidder Management", "done": True}, {"text": "One off", "done": False}]),
        _day(date=wed, goalItems=[{"text": "bidder management", "done": False}]),
    ])
    await migrate(True, db)
    week = await db["weeklyplans"].find_one({})
    assert week["weekStart"] == MON and week["year"] == 2026 and week["goal"] == {}
    assert [r["text"] for r in week["routines"]] == ["Bidder Management"]
    assert [g["text"] for g in week["weeklyGoals"]] == ["One off"]
    day = await db["dailyplans"].find_one({"date": TUE})
    assert set(day["results"]) == {week["routines"][0]["id"], week["weeklyGoals"][0]["id"]}
    assert day["submittedAt"] == datetime(2026, 11, 10, 22)
    plan = DailyPlan.model_validate(day)
    assert day_targets(plan, WeeklyPlan.model_validate(week), TUE.date()).bidsSelf == 12


class _Boom:
    """Database proxy whose dailyplans writes fail (a crash after the weekly writes)."""

    def __init__(self, db):
        self.db = db

    def __getitem__(self, name):
        if name != "dailyplans":
            return self.db[name]
        real = self.db[name]
        boom = type("C", (), {"find": lambda _s, *a, **k: real.find(*a, **k)})()

        async def fail(*a, **k):
            raise RuntimeError("crash")
        boom.update_one = fail
        return boom


async def test_crash_after_weekly_writes_then_rerun_keeps_ids_consistent():
    db = await _db()
    await _setup(db)
    with pytest.raises(RuntimeError):
        await migrate(True, _Boom(db))
    await migrate(True, db)
    week, day = await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})
    ids = {i["id"] for i in week["routines"] + week["weeklyGoals"]}
    assert day["results"] and set(day["results"]) <= ids


async def test_ticked_count_line_gets_its_target_as_value_and_renders_done():
    from types import SimpleNamespace

    from app.plan_auto import day_items
    from app.slack_bot import daily_plan_text

    db = await _db()
    await _setup(db, week=_week(dailyGoalItems=[{"text": "Send 50 emails", "done": False}]),
                 day=_day(goalItems=[{"text": "Send 50 emails", "done": True}]))
    await migrate(True, db)
    week = WeeklyPlan.model_validate(await db["weeklyplans"].find_one({}))
    plan = DailyPlan.model_validate(await db["dailyplans"].find_one({}))
    rid = week.routines[0].id
    assert (plan.results[rid].done, plan.results[rid].value, plan.results[rid].target) == (True, 50, 50)
    text = daily_plan_text(plan, SimpleNamespace(name="A", email="a@x.io", slackUserId="U1"),
                           items=day_items(week, plan, TUE.date()))
    assert "Send 50 emails    50 / 50  ✅" in text


async def test_ticked_count_line_without_target_stays_done():
    db = await _db()
    await _setup(db, week=_week(routines=[{"id": "r1", "text": "Send emails", "kind": "count", "target": None}],
                                weeklyGoals=[]),
                 day=_day(goalItems=[{"text": "send emails", "done": True}]))
    await migrate(True, db)
    day = await db["dailyplans"].find_one({})
    assert day["results"]["r1"]["done"] is True and day["results"]["r1"]["value"] is None


async def test_week_and_day_saved_after_deploy_still_get_legacy_lines():
    db = await _db()
    await _setup(db, week=_week(routines=[], weeklyGoals=[{"id": "g1", "text": "Ship the thing", "kind": "check"}],
                                dailyGoalItems=[{"text": "Bidder Management", "done": False}],
                                goalItems=[{"text": "Ship the thing", "done": False}]),
                 day=_day(results={}, goalItems=[{"text": "Bidder management", "done": True},
                                                {"text": "ship the thing", "done": True}]))
    await migrate(True, db)
    week, day = await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})
    assert [r["text"] for r in week["routines"]] == ["Bidder Management"]
    assert [g["id"] for g in week["weeklyGoals"]] == ["g1"]  # matched by text, not added twice
    rid = week["routines"][0]["id"]
    assert day["results"][rid]["done"] is True and day["results"]["g1"]["done"] is True
    counts = await migrate(True, db)  # still idempotent
    assert counts["weeklyMigrated"] == counts["dailyMigrated"] == 0
    assert (week, day) == (await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({}))


async def test_day_result_already_saved_is_kept():
    db = await _db()
    await _setup(db, week=_week(routines=[{"id": "r1", "text": "Bidder Management", "kind": "check"}], weeklyGoals=[]),
                 day=_day(results={"r1": {"done": False}}, goalItems=[{"text": "Bidder Management", "done": True}]))
    await migrate(True, db)
    assert (await db["dailyplans"].find_one({}))["results"]["r1"]["done"] is False


async def test_duplicate_weeks_pick_the_one_the_app_reads():
    db = await _db()
    old, new = datetime(2026, 11, 9, 8), datetime(2026, 11, 9, 9)
    await db["weeklyplans"].insert_many([
        _week(dailyGoalItems=[{"text": "Newer", "done": False}], updatedAt=new),
        _week(dailyGoalItems=[{"text": "Older", "done": False}], updatedAt=old),
    ])
    counts = await migrate(False, db)
    assert counts["duplicateWeeks"] == 1
    await migrate(True, db)
    newer = await db["weeklyplans"].find_one({"updatedAt": {"$ne": old}})
    assert [r["text"] for r in newer["routines"]] == ["Newer"]
    older = await db["weeklyplans"].find_one({"dailyGoalItems.text": "Older"})
    assert older["updatedAt"] == old and "routines" not in older
    from app.plan_auto import weekly_plans_for
    assert (await weekly_plans_for([USER], MON.date()))[USER].id == newer["_id"]


async def test_old_shape_day_is_folded_before_reading_lines():
    db = await _db()
    await _setup(db, week=_week(dailyGoalItems=[{"text": "Bidder Management", "done": False}]),
                 day={"userId": USER, "date": TUE, "year": 2026, "weekNumber": 46, "bidsHandsOn": 3,
                      "todayItems": ["bidder management"], "createdAt": datetime(2026, 11, 10, 20)})
    await migrate(True, db)
    week, day = await db["weeklyplans"].find_one({}), await db["dailyplans"].find_one({})
    assert day["results"] == {week["routines"][0]["id"]: {"done": True, "value": None, "target": None}}
    assert day["submittedAt"] == datetime(2026, 11, 10, 20)


async def test_empty_day_goal_falls_back_to_week_daily_goal():
    db = await _db()
    await _setup(db, week=_week(dailyGoal={"bidsSelf": 15, "bidsBidder": 0}, dailyGoalItems=[]),
                 day=_day(goal={}, goalItems=[]))
    await migrate(True, db)
    plan = DailyPlan.model_validate(await db["dailyplans"].find_one({}))
    weekly = WeeklyPlan.model_validate(await db["weeklyplans"].find_one({}))
    assert day_targets(plan, weekly, TUE.date()).bidsSelf == 15  # not 50 / 5
