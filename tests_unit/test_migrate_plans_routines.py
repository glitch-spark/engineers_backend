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
