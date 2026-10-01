"""Pure document mapping of scripts/migrate_plans_goal_done.py."""

import os
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from scripts.migrate_plans_goal_done import migrate_daily, migrate_weekly  # noqa: E402

CREATED = datetime(2026, 10, 3, 21)


def _daily(**kw):
    doc = {"_id": "d1", "userId": "u1", "date": datetime(2026, 10, 3), "year": 2026, "weekNumber": 40,
           "createdAt": CREATED}
    doc.update(kw)
    return doc


def test_migrate_regular_daily():
    doc = _daily(planType="regular", bidsHandsOn=5, bidsByBidder=2, interviewsDone=1, interviewsNew=4,
                 todayItems=["Fix resume"], planBidsHandsOn=6, planBidsByBidder=3,
                 planInterviewsScheduled=2, tomorrowItems=["Prep panel"])
    to_set, to_unset, next_goal = migrate_daily(doc)
    assert to_set["done"] == {"bidsSelf": 5, "bidsBidder": 2}
    assert to_set["doneOverrides"] == {"interviewsSelf": 1}
    assert to_set["goalItems"] == [{"text": "Fix resume", "done": True}]
    assert to_set["loggedAt"] == CREATED
    assert "goal" not in to_set
    assert sorted(to_unset) == sorted([
        "planType", "bidsHandsOn", "bidsByBidder", "interviewsDone", "interviewsNew", "todayItems",
        "planBidsHandsOn", "planBidsByBidder", "planInterviewsScheduled", "tomorrowItems",
    ])
    # Saturday's plan for tomorrow lands on Monday.
    assert next_goal == {
        "date": datetime(2026, 10, 5),
        "goal": {"bidsSelf": 6, "bidsBidder": 3, "interviewsSelf": 2},
        "goalItems": [{"text": "Prep panel", "done": False}],
    }


def test_migrate_custom_daily_text_to_notes_without_next_goal():
    to_set, to_unset, next_goal = migrate_daily(_daily(today="Shipped", tomorrow="Tests"))
    assert to_set["notes"] == "Shipped\n\nTests"
    assert to_set["loggedAt"] == CREATED
    assert sorted(to_unset) == ["today", "tomorrow"]
    assert next_goal is None


def test_migrate_daily_already_migrated_is_noop():
    assert migrate_daily(_daily(done={"bidsSelf": 1}, goal={})) == ({}, [], None)


def test_migrate_weekly():
    doc = {"_id": "w1", "userId": "u1", "year": 2026, "weekNumber": 40,
           "startDate": datetime(2026, 9, 28, 4), "endDate": datetime(2026, 10, 5, 3, 59),
           "metrics": [{"key": "bids", "target": 40, "actual": 31}, {"key": "interviews", "target": 5, "actual": 1}],
           "nextInterviewTarget": 3, "status": "reviewed", "content": "Plan", "result": "Recap"}
    to_set, to_unset = migrate_weekly(doc)
    assert to_set == {
        "weekStart": datetime(2026, 9, 28),
        "goal": {"bidsSelf": 40, "interviewsSelf": 5},
        "planNotes": "Plan",
        "recapNotes": "Recap",
    }
    assert sorted(to_unset) == ["content", "metrics", "nextInterviewTarget", "result", "status"]


def test_migrate_weekly_already_migrated_is_noop():
    doc = {"_id": "w1", "weekStart": datetime(2026, 9, 28), "goal": {"bidsSelf": 1}}
    assert migrate_weekly(doc) == ({}, [])
