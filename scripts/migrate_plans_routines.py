"""Move goal lines to routines, weekly goals and per-day results.

Run once after deploying routines:
  python -m scripts.migrate_plans_routines          # dry run (default)
  python -m scripts.migrate_plans_routines --apply

Weekly plans: dailyGoalItems -> routines; goalItems that match no routine -> weeklyGoals.
Daily plans (old shapes folded first): each goalItems line -> results[id] of the matching routine / goal (done kept;
a ticked count / hours line with a target gets value = target); an unmatched line is appended to that week's
weeklyGoals; targetOverride = the day's stored goal when it has bids, else the week's legacy dailyGoal when it has
bids; submittedAt = loggedAt.
Lines match on casefold + collapsed whitespace + trailing '.' / ':' stripped. Numbers stay in the text.
Several weekly plans for one (user, week): the most recently updated one (what the app reads) is migrated.
Idempotent: lines already in the week (by text) and results the day already has are left as they are, so weeks
and days saved after deploy still get their legacy lines.
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
from app.models.daily_plan import fold_legacy_daily  # noqa: E402
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


def _result(item: dict, done: bool) -> dict:
    """A ticked count / hours line with a target is done at it (value = target); else just the done flag."""
    if done and item["kind"] != "check" and item.get("target"):
        return ItemResult(done=True, value=item["target"], target=item["target"]).model_dump()
    return ItemResult(done=done).model_dump()


async def migrate(apply: bool, database=None) -> dict:
    if database is None:
        database = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    daily, weekly = database["dailyplans"], database["weeklyplans"]
    counts = Counter(weeklyMigrated=0, dailyMigrated=0, unmatchedLinesToGoals=0, weeksCreated=0, skipped=0,
                     droppedDoneWeeklyGoals=0, duplicateWeeks=0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # One plan per (userId, Monday date), the one plan_auto.weekly_plans_for reads: the most recently updated
    # (a document without updatedAt reads as updated now there, so it wins here too).
    chosen: dict[tuple, dict] = {}
    for doc in sorted([d async for d in weekly.find({})], key=lambda d: d.get("updatedAt") or datetime.max):
        start = fold_legacy_weekly(doc).get("weekStart")
        key = (doc["userId"], start.date() if start else None)
        counts["duplicateWeeks"] += key in chosen
        chosen[key] = doc

    # key -> {"routines", "goals", "dailyGoal", "dirty", "id" | "new": doc to insert}. Legacy lines are added to
    # what the week already has (saved after deploy, or migrated before) unless their text is already there.
    weeks: dict[tuple, dict] = {}
    for key, doc in chosen.items():
        routines, goals = list(doc.get("routines") or []), list(doc.get("weeklyGoals") or [])
        known = {_key(i["text"]) for i in routines + goals}
        added = 0
        for field, into in (("dailyGoalItems", routines), ("goalItems", goals)):
            for line in _lines(doc, field):
                if field == "goalItems" and "routines" not in doc:
                    counts["droppedDoneWeeklyGoals"] += bool(line.get("done"))
                if _key(line["text"]) not in known:
                    known.add(_key(line["text"]))
                    into.append(parse_item(line["text"]).model_dump())
                    added += 1
        dirty = bool(added) or "routines" not in doc
        counts["weeklyMigrated" if dirty else "skipped"] += 1
        weeks[key] = {"routines": routines, "goals": goals, "id": doc["_id"], "dirty": dirty,
                      "dailyGoal": doc.get("dailyGoal") or {}}

    days = [fold_legacy_daily(d) async for d in daily.find({}).sort("date", 1)]

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
            "dailyGoal": {},
            "dirty": True,
            "new": {"userId": key[0], "weekStart": start, "year": year, "weekNumber": week_number,
                    "startDate": start, "endDate": start + timedelta(days=7) - timedelta(milliseconds=1),
                    "goal": {}, "goalItems": [], "dailyGoal": {}, "dailyGoalItems": [], "recapNotes": "",
                    "createdAt": now},
        }
        counts["weeksCreated"] += 1

    # Days: each legacy line maps to its routine / goal unless the day already has a result for it (saved after
    # deploy, or migrated before).
    day_updates = []
    for doc in days:
        week = weeks.get((doc["userId"], monday_of(doc["date"].date())))
        results = dict(doc.get("results") or {})
        for line in _lines(doc, "goalItems"):
            match = next((i for i in week["routines"] + week["goals"] if _key(i["text"]) == _key(line["text"])), None)
            if match is None:
                match = parse_item(line["text"]).model_dump()
                week["goals"].append(match)
                week["dirty"] = True
                counts["unmatchedLinesToGoals"] += 1
            results.setdefault(match["id"], _result(match, bool(line.get("done"))))
        to_set: dict = {}
        if results != (doc.get("results") or {}) or "results" not in doc:
            to_set["results"] = results
        if not doc.get("targetOverride"):
            # The day's own goal, else the week's legacy daily goal (what the day showed before routines).
            for goal in (doc.get("goal") or {}, (week or {}).get("dailyGoal") or {}):
                if any(goal.get(f) for f in BID_FIELDS):
                    to_set["targetOverride"] = goal
                    break
        if doc.get("loggedAt") and not doc.get("submittedAt"):
            to_set["submittedAt"] = doc["loggedAt"]
        if to_set:
            counts["dailyMigrated"] += 1
            day_updates.append((doc["_id"], {**to_set, "updatedAt": now}))

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
