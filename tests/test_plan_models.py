"""Goal vs Done plan models and their legacy fold.

Integration suite because Beanie documents need an initialised database
(the `app` fixture) before they can be constructed.
"""

from datetime import date, datetime

import pytest
from beanie import PydanticObjectId

from app.models.daily_plan import DailyPlan
from app.models.plan_common import Counts, monday_of, next_working_day
from app.models.weekly_plan import WeeklyPlan

USER = PydanticObjectId()


def _legacy_daily(**kw):
    base = {
        "userId": USER,
        "date": datetime(2026, 9, 30),
        "year": 2026,
        "weekNumber": 40,
        "createdAt": datetime(2026, 9, 30, 21, 0),
    }
    base.update(kw)
    return base


def test_fold_legacy_daily_regular():
    plan = DailyPlan.model_validate(_legacy_daily(
        planType="regular",
        bidsHandsOn=4,
        bidsByBidder=2,
        interviewsDone=1,
        interviewsNew=3,
        todayItems=["Fix resume"],
        planBidsHandsOn=6,
        tomorrowItems=["Call recruiter"],
    ))
    assert (plan.done.bidsSelf, plan.done.bidsBidder) == (4, 2)
    # Interviews are never typed: the old count is dropped.
    assert (plan.done.interviewsSelf, plan.done.interviewsCaller) == (0, 0)
    assert [(i.text, i.done) for i in plan.goalItems] == [("Fix resume", True)]
    assert plan.loggedAt == datetime(2026, 9, 30, 21, 0)
    assert plan.goal.bidsSelf == 0  # tomorrow's plan belongs to the next day (migration)
    assert not hasattr(plan, "bidsHandsOn") and not hasattr(plan, "doneOverrides")


def test_fold_legacy_daily_custom_text_to_notes():
    plan = DailyPlan.model_validate(_legacy_daily(today="Shipped filter", tomorrow="Write tests"))
    assert plan.notes == "Shipped filter\n\nWrite tests"
    assert plan.loggedAt == datetime(2026, 9, 30, 21, 0)


def test_fold_legacy_daily_empty_is_not_logged():
    plan = DailyPlan.model_validate(_legacy_daily(planType="regular"))
    assert plan.loggedAt is None


def test_fold_legacy_weekly_metrics_and_text():
    plan = WeeklyPlan.model_validate({
        "userId": USER,
        "weekNumber": 40,
        "year": 2026,
        "startDate": datetime(2026, 9, 28, 4, 0),
        "endDate": datetime(2026, 10, 5, 3, 59),
        "metrics": [
            {"key": "bids", "label": "Job applies", "target": 40, "actual": 31},
            {"key": "interviews", "label": "Interviews", "target": 5, "actual": 2},
            {"key": "outreach", "label": "Outreach", "target": 10, "actual": 3},
        ],
        "nextInterviewTarget": 4,
        "status": "reviewed",
        "content": "Plan text",
        "result": "Recap text",
    })
    assert plan.weekStart == datetime(2026, 9, 28)
    assert plan.goal.bidsSelf == 40 and plan.goal.interviewsSelf == 5
    assert plan.recapNotes == "Plan text\n\nRecap text"
    assert not hasattr(plan, "metrics") and not hasattr(plan, "planNotes")


def test_new_shape_untouched():
    plan = DailyPlan.model_validate(_legacy_daily(
        goal={"bidsSelf": 6, "bidsBidder": 4},
        done={"bidsSelf": 5},
        notes="n",
        loggedAt=datetime(2026, 9, 30, 22, 0),
    ))
    assert plan.goal.bidsSelf == 6 and plan.done.bidsSelf == 5
    assert plan.loggedAt == datetime(2026, 9, 30, 22, 0)


def test_counts_reject_negative():
    with pytest.raises(ValueError):
        Counts(bidsSelf=-1)


def test_next_working_day_saturday_to_monday():
    assert next_working_day(date(2026, 10, 3)) == date(2026, 10, 5)
    assert next_working_day(date(2026, 9, 30)) == date(2026, 10, 1)
    assert next_working_day(date(2026, 10, 4)) == date(2026, 10, 5)


def test_monday_of_sunday_is_preceding_monday():
    assert monday_of(date(2026, 10, 4)) == date(2026, 9, 28)
    assert monday_of(date(2026, 9, 28)) == date(2026, 9, 28)
    assert monday_of(date(2026, 10, 3)) == date(2026, 9, 28)
