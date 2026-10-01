"""Integration tests for app.plan_auto (needs the throwaway test database)."""

from datetime import date, datetime

from beanie import PydanticObjectId

from app.models import DailyPlan, Interview, WeeklyPlan
from app.plan_auto import interview_counts, week_snapshot


async def test_interview_counts_by_day_self_and_caller(staff_user, staff_user_2):
    account_id = PydanticObjectId()
    await Interview(
        accountId=account_id,
        createdBy=staff_user.id,
        scheduledAt=datetime(2026, 10, 1, 15),
        stage="tech_round_1",
        status="scheduled",
        # Rounds carry their own caller: the intro was taken alone, the tech round with a caller.
        stageHistory=[
            {"stage": "intro", "at": datetime(2026, 9, 29, 9), "scheduledAt": datetime(2026, 9, 29, 9),
             "status": "completed"},
            {"stage": "tech_round_1", "at": datetime(2026, 10, 1, 15), "scheduledAt": datetime(2026, 10, 1, 15),
             "status": "scheduled", "caller": {"enabled": True, "startsAt": datetime(2026, 10, 1, 15)}},
        ],
    ).insert()
    await Interview(
        accountId=account_id,
        createdBy=staff_user_2.id,
        scheduledAt=datetime(2026, 10, 1, 10),
        stage="intro",
        status="scheduled",
    ).insert()

    counts = await interview_counts([staff_user.id, staff_user_2.id], date(2026, 9, 28), date(2026, 10, 4))

    mine = counts[staff_user.id]
    assert mine[date(2026, 9, 29)]["self"] == 1
    assert mine[date(2026, 9, 29)]["stages"]["self"] == {"intro": 1}
    assert mine[date(2026, 10, 1)]["caller"] == 1
    assert mine[date(2026, 10, 1)]["stages"]["caller"] == {"tech": 1}
    assert counts[staff_user_2.id][date(2026, 10, 1)]["self"] == 1


async def test_week_snapshot_reads_legacy_weekly_without_weekStart(staff_user):
    await WeeklyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id,
        "weekNumber": 40,
        "year": 2026,
        "startDate": datetime(2026, 9, 28, 4, 0),
        "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [{"key": "bids", "label": "Job applies", "target": 40, "actual": 0}],
        "content": "plan",
        "result": "",
        "createdAt": datetime(2026, 9, 28),
        "updatedAt": datetime(2026, 9, 28),
    })
    await DailyPlan(
        userId=staff_user.id, date=datetime(2026, 9, 29), year=2026, weekNumber=40,
        done={"bidsSelf": 6, "bidsBidder": 2}, loggedAt=datetime(2026, 9, 29, 20),
    ).insert()

    snap = (await week_snapshot([staff_user.id], date(2026, 9, 28)))[staff_user.id]
    assert snap["plan"] is not None and snap["plan"].goal.bidsSelf == 40
    assert snap["done"].bidsSelf == 6 and snap["done"].bidsBidder == 2
    assert snap["lastLogged"] == date(2026, 9, 29)


async def test_week_snapshot_user_without_anything(staff_user):
    snap = (await week_snapshot([staff_user.id], date(2026, 9, 28)))[staff_user.id]
    assert snap["plan"] is None and snap["days"] == [] and snap["lastLogged"] is None
    assert snap["done"].bidsSelf == 0
