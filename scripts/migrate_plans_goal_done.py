"""Move daily and weekly plans to the Goal vs Done shape.

Run once after deploying Goal vs Done plans:
  python -m scripts.migrate_plans_goal_done          # dry run (default)
  python -m scripts.migrate_plans_goal_done --apply

Idempotent. Until it runs, the models fold old documents on read, so the app
works either way.

Daily plans:
- bids hands-on / by bidder -> done.bidsSelf / done.bidsBidder
- typed interview counts are dropped (interviews now come from the
  Interviews page)
- today's items -> ticked goal items; free text -> notes; loggedAt = createdAt
- the plan for tomorrow (bids and items) -> the next working day's goal (a goal-only entry is
  created when that day has none; an existing non-empty goal is never
  overwritten)

Weekly plans:
- weekStart from startDate; bids / interviews targets -> goal.bidsSelf /
  goal.interviewsSelf; plan and recap text -> recapNotes; other
  metrics, actuals, status and nextInterviewTarget are dropped
- several plans for the same user and week are reported, not merged
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.alerts import app_week_key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models.daily_plan import _LEGACY_KEYS as DAILY_LEGACY_KEYS  # noqa: E402
from app.models.daily_plan import fold_legacy_daily  # noqa: E402
from app.models.plan_common import next_working_day  # noqa: E402
from app.models.weekly_plan import _LEGACY_KEYS as WEEKLY_LEGACY_KEYS  # noqa: E402
from app.models.weekly_plan import fold_legacy_weekly  # noqa: E402

_DAILY_FIELDS = ("done", "goalItems", "notes", "loggedAt")


def migrate_daily(doc: dict) -> tuple[dict, list, dict | None]:
    """($set, $unset keys, next working day's goal or None) for one daily plan."""
    legacy = [key for key in DAILY_LEGACY_KEYS if key in doc]
    if not legacy:
        return {}, [], None
    to_set: dict = {}
    if "done" not in doc:
        folded = fold_legacy_daily(doc)
        to_set = {key: folded[key] for key in _DAILY_FIELDS if folded.get(key) is not None}

    goal = {
        key: int(doc.get(old) or 0)
        for key, old in (
            ("bidsSelf", "planBidsHandsOn"),
            ("bidsBidder", "planBidsByBidder"),
        )
        if doc.get(old)
    }
    items = [{"text": str(t).strip()[:200], "done": False} for t in doc.get("tomorrowItems") or [] if str(t).strip()]
    next_goal = None
    if goal or items:
        nxt = next_working_day(doc["date"].date())
        next_goal = {"date": datetime(nxt.year, nxt.month, nxt.day), "goal": goal, "goalItems": items}
    return to_set, legacy, next_goal


def migrate_weekly(doc: dict) -> tuple[dict, list]:
    """($set, $unset keys) for one weekly plan."""
    legacy = [key for key in WEEKLY_LEGACY_KEYS if key in doc]
    if not legacy and "weekStart" in doc:
        return {}, []
    folded = fold_legacy_weekly(doc)
    to_set = {"weekStart": folded["weekStart"]} if "weekStart" not in doc else {}
    if legacy and "goal" not in doc:
        to_set.update(goal=folded["goal"], recapNotes=folded["recapNotes"])
    return to_set, legacy


def _merge_items(own: list, carried: list) -> list:
    """This day's items first, then items carried from the day before (no duplicates, max 20)."""
    seen = {item["text"] for item in own}
    return (own + [item for item in carried if item.get("text") not in seen])[:20]


def _goal_is_empty(doc: dict) -> bool:
    goal = doc.get("goal") or {}
    has_counts = any(isinstance(v, int) and v for v in goal.values()) or any(
        isinstance(v, dict) and v for v in goal.values()
    )
    return not has_counts and not doc.get("goalItems")


def _update(to_set: dict, to_unset: list) -> dict:
    update: dict = {}
    if to_set:
        update["$set"] = to_set
    if to_unset:
        update["$unset"] = {key: "" for key in to_unset}
    return update


async def run(apply: bool, database=None) -> dict:
    if database is None:
        database = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    daily = database["dailyplans"]
    weekly = database["weeklyplans"]
    counts = Counter(dailyMigrated=0, nextDayGoalsCreated=0, nextDayGoalsSkipped=0,
                     weeklyMigrated=0, weeklyDuplicates=0, alreadyMigrated=0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    daily_legacy = {"$or": [{key: {"$exists": True}} for key in DAILY_LEGACY_KEYS]}
    counts["alreadyMigrated"] += await daily.count_documents({"$nor": daily_legacy["$or"]})
    async for doc in daily.find(daily_legacy).sort("date", 1):
        to_set, to_unset, next_goal = migrate_daily(doc)
        counts["dailyMigrated"] += 1
        if apply:
            if "goalItems" in to_set:
                # The previous day's plan for tomorrow may already have put
                # items on this day (docs run in date order): keep them.
                current = await daily.find_one({"_id": doc["_id"]}, {"goalItems": 1})
                to_set["goalItems"] = _merge_items(to_set["goalItems"], (current or {}).get("goalItems") or [])
            await daily.update_one({"_id": doc["_id"]}, _update({**to_set, "updatedAt": now}, to_unset))
        if next_goal is None:
            continue
        target = await daily.find_one({"userId": doc["userId"], "date": next_goal["date"]})
        if target is not None and not _goal_is_empty(target):
            counts["nextDayGoalsSkipped"] += 1
            continue
        counts["nextDayGoalsCreated"] += 1
        if not apply:
            continue
        if target is None:
            year, week_number, _key = app_week_key(next_goal["date"])
            await daily.insert_one({
                "userId": doc["userId"], "date": next_goal["date"], "year": year, "weekNumber": week_number,
                "goal": next_goal["goal"], "goalItems": next_goal["goalItems"],
                "done": {}, "notes": "", "loggedAt": None,
                "createdAt": now, "updatedAt": now,
            })
        else:
            await daily.update_one(
                {"_id": target["_id"]},
                {"$set": {"goal": next_goal["goal"], "goalItems": next_goal["goalItems"], "updatedAt": now}},
            )

    weeks: Counter = Counter()
    async for doc in weekly.find({}):
        to_set, to_unset = migrate_weekly(doc)
        week_start = to_set.get("weekStart", doc.get("weekStart"))
        weeks[(doc["userId"], week_start)] += 1
        if not to_set and not to_unset:
            counts["alreadyMigrated"] += 1
            continue
        counts["weeklyMigrated"] += 1
        if apply:
            await weekly.update_one({"_id": doc["_id"]}, _update({**to_set, "updatedAt": now}, to_unset))
    counts["weeklyDuplicates"] = sum(n - 1 for n in weeks.values() if n > 1)

    mode = "applied" if apply else "dry run"
    print(f"[{mode}] " + ", ".join(f"{key} {value}" for key, value in counts.items()))
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    asyncio.run(run(apply=parser.parse_args().apply))


if __name__ == "__main__":
    main()
