"""scripts/migrate_plans_goal_done.py against the test database."""

from datetime import datetime

from app.models import DailyPlan, WeeklyPlan
from scripts.migrate_plans_goal_done import run


def _daily_col():
    return DailyPlan.get_motor_collection()


def _weekly_col():
    return WeeklyPlan.get_motor_collection()


async def _seed(user):
    await _daily_col().insert_one({
        "userId": user.id, "date": datetime(2026, 9, 30), "year": 2026, "weekNumber": 40,
        "planType": "regular", "bidsHandsOn": 5, "bidsByBidder": 1, "interviewsDone": 1,
        "todayItems": ["Fix resume"], "planBidsHandsOn": 7, "tomorrowItems": ["Prep"],
        "createdAt": datetime(2026, 9, 30, 21), "updatedAt": datetime(2026, 9, 30, 21),
    })
    await _weekly_col().insert_one({
        "userId": user.id, "year": 2026, "weekNumber": 40,
        "startDate": datetime(2026, 9, 28, 4), "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [{"key": "bids", "label": "Job applies", "target": 40, "actual": 0}],
        "content": "Plan", "result": "", "status": "planned",
        "createdAt": datetime(2026, 9, 28), "updatedAt": datetime(2026, 9, 28),
    })


def _db():
    return DailyPlan.get_motor_collection().database


async def test_dry_run_writes_nothing(staff_user):
    await _seed(staff_user)
    before = [d async for d in _daily_col().find({})] + [d async for d in _weekly_col().find({})]
    counts = await run(apply=False, database=_db())
    after = [d async for d in _daily_col().find({})] + [d async for d in _weekly_col().find({})]
    assert after == before
    assert counts["dailyMigrated"] == 1
    assert counts["nextDayGoalsCreated"] == 1
    assert counts["weeklyMigrated"] == 1


async def test_apply_then_second_run_is_noop(staff_user):
    await _seed(staff_user)
    counts = await run(apply=True, database=_db())
    assert counts["dailyMigrated"] == 1 and counts["weeklyMigrated"] == 1
    assert counts["nextDayGoalsCreated"] == 1

    wed = await _daily_col().find_one({"date": datetime(2026, 9, 30)})
    assert "bidsHandsOn" not in wed and wed["done"] == {"bidsSelf": 5, "bidsBidder": 1}
    assert wed["doneOverrides"] == {"interviewsSelf": 1}
    thu = await DailyPlan.find_one({"date": datetime(2026, 10, 1)})
    assert thu.goal.bidsSelf == 7 and [i.text for i in thu.goalItems] == ["Prep"]
    assert thu.loggedAt is None and thu.year == 2026
    week = await _weekly_col().find_one({})
    assert week["weekStart"] == datetime(2026, 9, 28) and week["goal"] == {"bidsSelf": 40}
    assert "metrics" not in week and week["planNotes"] == "Plan"

    again = await run(apply=True, database=_db())
    assert again["dailyMigrated"] == 0 and again["weeklyMigrated"] == 0
    assert again["nextDayGoalsCreated"] == 0 and again["nextDayGoalsSkipped"] == 0


async def test_next_day_goal_not_overwritten(staff_user):
    await _seed(staff_user)
    await _daily_col().insert_one({
        "userId": staff_user.id, "date": datetime(2026, 10, 1), "year": 2026, "weekNumber": 40,
        "goal": {"bidsSelf": 2}, "goalItems": [], "done": {}, "doneOverrides": {}, "notes": "",
        "loggedAt": None, "createdAt": datetime(2026, 10, 1), "updatedAt": datetime(2026, 10, 1),
    })
    counts = await run(apply=True, database=_db())
    assert counts["nextDayGoalsSkipped"] == 1 and counts["nextDayGoalsCreated"] == 0
    thu = await _daily_col().find_one({"date": datetime(2026, 10, 1)})
    assert thu["goal"] == {"bidsSelf": 2}


async def test_weekly_duplicates_reported(staff_user):
    await _seed(staff_user)
    await _weekly_col().insert_one({
        "userId": staff_user.id, "year": 2026, "weekNumber": 40,
        "startDate": datetime(2026, 9, 28), "endDate": datetime(2026, 10, 4),
        "metrics": [], "content": "dup", "createdAt": datetime(2026, 9, 29), "updatedAt": datetime(2026, 9, 29),
    })
    counts = await run(apply=False, database=_db())
    assert counts["weeklyDuplicates"] == 1
