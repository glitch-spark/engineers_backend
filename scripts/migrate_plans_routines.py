"""Move goal lines to routines, weekly goals and per-day results.

Run once after deploying routines:
  python -m scripts.migrate_plans_routines          # dry run (default)
  python -m scripts.migrate_plans_routines --apply

Weekly plans: dailyGoalItems -> routines; goalItems that match no routine -> weeklyGoals.
Daily plans: each goalItems line -> results[id] of the matching routine / goal (done kept); an unmatched line is
appended to that week's weeklyGoals; targetOverride = the day's stored goal when it has bids; submittedAt = loggedAt.
Lines match on casefold + collapsed whitespace + trailing '.' / ':' stripped. Numbers stay in the text.
Idempotent: weekly plans that have `routines` and daily plans that have `results` are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.alerts import app_week_key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models.plan_common import BID_FIELDS, ItemResult, PlanItem, monday_of  # noqa: E402
from app.models.weekly_plan import fold_legacy_weekly  # noqa: E402

log = logging.getLogger(__name__)

_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\b", re.I)
_MINUTES = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes?)\b", re.I)
_TIMES = re.compile(r"[x×]\s*(\d+)", re.I)
_COUNT_WORD = re.compile(r"(\d+)\s+[^\W\d_]")


def parse_item(text: str) -> PlanItem:
    """`x 250` / `×250` / `250 <word>` -> count; `2h` / `1.5h` / `30min` -> hours; else check. Text is kept."""
    text = text.strip()
    if m := _HOURS.search(text):
        return PlanItem(text=text, kind="hours", target=float(m[1]))
    if m := _MINUTES.search(text):
        return PlanItem(text=text, kind="hours", target=float(m[1]) / 60)
    if m := _TIMES.search(text):
        return PlanItem(text=text, kind="count", target=int(m[1]))
    if matches := _COUNT_WORD.findall(text):
        return PlanItem(text=text, kind="count", target=int(matches[-1]))  # last: "(...)" parts may hold numbers
    return PlanItem(text=text, kind="check")


def _key(text: str) -> str:
    return " ".join(str(text).casefold().split()).rstrip(".: ")


def _lines(doc: dict, field: str) -> list[dict]:
    return [i for i in doc.get(field) or [] if isinstance(i, dict) and str(i.get("text") or "").strip()]


async def migrate(apply: bool, database=None) -> dict:
    if database is None:
        database = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    daily, weekly = database["dailyplans"], database["weeklyplans"]
    counts = Counter(weeklyMigrated=0, dailyMigrated=0, unmatchedLinesToGoals=0, weeksCreated=0, skipped=0,
                     droppedDoneWeeklyGoals=0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # (userId, Monday date) -> {"routines", "goals", "dirty", "id" | "new": doc to insert}
    weeks: dict[tuple, dict] = {}
    async for doc in weekly.find({}):
        start = fold_legacy_weekly(doc).get("weekStart")
        key = (doc["userId"], start.date() if start else None)
        if "routines" in doc:
            counts["skipped"] += 1
            weeks.setdefault(key, {"routines": doc["routines"], "goals": doc.get("weeklyGoals") or [], "id": doc["_id"], "dirty": False})
            continue
        routines = [parse_item(i["text"]).model_dump() for i in _lines(doc, "dailyGoalItems")]
        known = {_key(r["text"]) for r in routines}
        goals = []
        for line in _lines(doc, "goalItems"):
            counts["droppedDoneWeeklyGoals"] += bool(line.get("done"))
            if _key(line["text"]) not in known:
                known.add(_key(line["text"]))
                goals.append(parse_item(line["text"]).model_dump())
        counts["weeklyMigrated"] += 1
        weeks.setdefault(key, {"routines": routines, "goals": goals, "id": doc["_id"], "dirty": True})

    days = [d async for d in daily.find({"results": {"$exists": False}}).sort("date", 1)]

    # A week with day lines but no weekly plan: create it. Lines on >= 2 of its days are routines, the rest goals.
    by_week: dict[tuple, list] = {}
    for doc in days:
        by_week.setdefault((doc["userId"], monday_of(doc["date"].date())), []).append(doc)
    for key, docs in by_week.items():
        if key in weeks:
            continue
        seen: dict[str, set] = {}
        first: dict[str, str] = {}
        for doc in docs:
            for line in _lines(doc, "goalItems"):
                seen.setdefault(_key(line["text"]), set()).add(doc["date"])
                first.setdefault(_key(line["text"]), line["text"])
        if not seen:
            continue
        items = {k: parse_item(first[k]).model_dump() for k in seen}
        start = datetime.combine(key[1], datetime.min.time())
        year, week_number, _k = app_week_key(start)
        weeks[key] = {
            "routines": [items[k] for k in seen if len(seen[k]) >= 2],
            "goals": [items[k] for k in seen if len(seen[k]) < 2],
            "dirty": True,
            "new": {"userId": key[0], "weekStart": start, "year": year, "weekNumber": week_number,
                    "startDate": start, "endDate": start + timedelta(days=7) - timedelta(milliseconds=1),
                    "goal": {}, "goalItems": [], "dailyGoal": {}, "dailyGoalItems": [], "recapNotes": "",
                    "createdAt": now},
        }
        counts["weeksCreated"] += 1

    day_updates = []
    for doc in days:
        week = weeks.get((doc["userId"], monday_of(doc["date"].date())))
        results: dict = {}
        for line in _lines(doc, "goalItems"):
            match = next((i for i in week["routines"] + week["goals"] if _key(i["text"]) == _key(line["text"])), None)
            if match is None:
                match = parse_item(line["text"]).model_dump()
                week["goals"].append(match)
                week["dirty"] = True
                counts["unmatchedLinesToGoals"] += 1
            results[match["id"]] = ItemResult(done=bool(line.get("done"))).model_dump()
        to_set = {"results": results, "updatedAt": now}
        goal = doc.get("goal") or {}
        if any(goal.get(f) for f in BID_FIELDS) and not doc.get("targetOverride"):
            to_set["targetOverride"] = goal
        if doc.get("loggedAt") and not doc.get("submittedAt"):
            to_set["submittedAt"] = doc["loggedAt"]
        counts["dailyMigrated"] += 1
        day_updates.append((doc["_id"], to_set))

    if apply:
        # Weeks first: a crash between the two leaves days unmigrated (a rerun maps them to the stored week ids),
        # never days whose result ids no week has.
        for week in weeks.values():
            if not week["dirty"]:
                continue
            fields = {"routines": week["routines"], "weeklyGoals": week["goals"], "updatedAt": now}
            if "new" in week:
                await weekly.insert_one({**week["new"], **fields})
            else:
                await weekly.update_one({"_id": week["id"]}, {"$set": fields})
        for _id, to_set in day_updates:
            await daily.update_one({"_id": _id}, {"$set": to_set})
    if counts["droppedDoneWeeklyGoals"]:
        log.warning("dropped the done flag of %d weekly goalItems", counts["droppedDoneWeeklyGoals"])
    mode = "applied" if apply else "dry run"
    print(f"[{mode}] " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate(apply=parser.parse_args().apply))


if __name__ == "__main__":
    main()
