"""Daily-bids alert reads the weekly plan's Goal (Goal vs Done plans)."""

from datetime import datetime, timezone

from app.alerts_service import _tick_daily_bids
from app.models import DailyPlan, Notification, ResumeApplication, WeeklyPlan

NOW = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)  # Thursday


async def _resume_apps(user, n):
    await ResumeApplication.get_motor_collection().insert_many([
        {"userId": user.id, "companyName": "Acme", "companyNameLower": "acme", "jobDescription": "x",
         "createdAt": datetime(2026, 10, 1, 10, i), "updatedAt": datetime(2026, 10, 1, 10, i)}
        for i in range(n)
    ])


async def test_daily_bids_alert_uses_weekly_goal(staff_user):
    await WeeklyPlan(
        userId=staff_user.id, weekStart=datetime(2026, 9, 28), year=2026, weekNumber=40,
        startDate=datetime(2026, 9, 28), endDate=datetime(2026, 10, 4, 23, 59),
        goal={"bidsSelf": 40, "bidsBidder": 10},
    ).insert()
    await _resume_apps(staff_user, 10)  # weekly goal 50 -> daily pace 10

    assert await _tick_daily_bids([staff_user], now=NOW) == 1
    assert await Notification.find({"kind": "daily_bids"}).count() == 1


async def test_daily_bids_alert_reads_unmigrated_weekly_plan(staff_user):
    await WeeklyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id, "year": 2026, "weekNumber": 40,
        "startDate": datetime(2026, 9, 28, 4), "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [{"key": "bids", "label": "Job applies", "target": 50, "actual": 0}],
        "createdAt": datetime(2026, 9, 28), "updatedAt": datetime(2026, 9, 28),
    })
    await _resume_apps(staff_user, 10)
    assert await _tick_daily_bids([staff_user], now=NOW) == 1


async def test_daily_bids_alert_without_plan_or_goal_is_quiet(staff_user):
    assert await _tick_daily_bids([staff_user], now=NOW) == 0
    await WeeklyPlan(
        userId=staff_user.id, weekStart=datetime(2026, 9, 28), year=2026, weekNumber=40,
        startDate=datetime(2026, 9, 28), endDate=datetime(2026, 10, 4, 23, 59),
    ).insert()
    await _resume_apps(staff_user, 10)
    assert await _tick_daily_bids([staff_user], now=NOW) == 0


async def test_daily_bids_alert_uses_the_days_own_target(staff_user):
    await WeeklyPlan(
        userId=staff_user.id, weekStart=datetime(2026, 9, 28), year=2026, weekNumber=40,
        startDate=datetime(2026, 9, 28), endDate=datetime(2026, 10, 4, 23, 59),
        goal={"bidsSelf": 40, "bidsBidder": 10},
    ).insert()
    await DailyPlan(userId=staff_user.id, date=datetime(2026, 10, 1), year=2026, weekNumber=40,
                    targetOverride={"bidsSelf": 20}).insert()
    await _resume_apps(staff_user, 10)  # the week's pace (10) is met, the day's own 20 is not
    assert await _tick_daily_bids([staff_user], now=NOW) == 0
