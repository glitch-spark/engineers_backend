"""Automatic Goal vs Done values for daily and weekly plans.

- Interviews Done always comes from interview records: each round held on a
  local day, split into self vs caller rounds (`split_rounds_by_day`).
- A day's Done is its typed bids plus those interviews.
- A week's Done sums the bids of its logged days and its interviews.

Nothing here writes to the database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rounds import load_interviews_for_window, split_rounds_by_day
from app.models import DailyPlan, WeeklyPlan
from app.models.plan_common import BID_FIELDS, ChecklistItem, Counts, bids_of


def empty_day() -> dict:
    return {"self": 0, "caller": 0, "stages": {"self": {}, "caller": {}}, "rounds": {"self": {}, "caller": {}}}


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def interview_counts(user_ids: list, start: date, end: date) -> dict:
    """{userId: {local date: {"self", "caller", "stages": {...}, "rounds": {...}}}}.

    `stages` counts rounds by breakdown group and `rounds` by exact stage, each
    split into {"self": {...}, "caller": {...}}.
    """
    tz = app_tz()
    start_utc = local_day_start_utc(start, tz)
    end_utc = local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    out: dict = {uid: {} for uid in user_ids}
    for iv in await load_interviews_for_window(user_ids, start_utc, end_utc):
        days = out.setdefault(iv.createdBy, {})
        for day, group, is_caller, stage in split_rounds_by_day(iv, start_utc, end_utc, tz):
            who = "caller" if is_caller else "self"
            entry = days.setdefault(day, empty_day())
            entry[who] += 1
            entry["stages"][who][group] = entry["stages"][who].get(group, 0) + 1
            entry["rounds"][who][stage] = entry["rounds"][who].get(stage, 0) + 1
    return out


async def interviews_by_day(user_ids: list, start: date, end: date) -> dict:
    """{userId: {date: interviews held (self + caller)}} over [start, end]."""
    counts = await interview_counts(user_ids, start, end)
    return {
        uid: {d: e["self"] + e["caller"] for d, e in counts.get(uid, {}).items()}
        for uid in user_ids
    }


def effective_daily_done(plan: Optional[Any], auto_day: dict) -> Counts:
    """A day's Done: typed bids (when there is a plan) and its interviews."""
    return Counts(
        **(bids_of(plan.done) if plan else {}),
        interviewsSelf=auto_day["self"],
        interviewsCaller=auto_day["caller"],
    )


def day_goal(plan: Optional[Any], weekly: Optional[Any]) -> tuple[Counts, list[ChecklistItem]]:
    """A day's goal: the bids and lines the day keeps its own copy of (once
    followed up, or carried over from older data), otherwise the week's daily
    goal. An empty copy — e.g. followed up before the week had a daily goal —
    falls back to the week's, so the day isn't compared against 0."""
    week_bids = Counts(**bids_of(weekly.dailyGoal)) if weekly else Counts()
    week_items = [ChecklistItem(text=i.text) for i in weekly.dailyGoalItems] if weekly else []
    if plan is None:
        return week_bids, week_items
    has_bids = bool(plan.goal.bidsSelf or plan.goal.bidsBidder)
    goal = Counts(**bids_of(plan.goal)) if has_bids else week_bids
    return goal, list(plan.goalItems) if plan.goalItems else week_items


def weekly_done(days: list, week_interviews: dict) -> Counts:
    """Week Done from (date, daily Done, logged) tuples and the week's interview totals.

    Only logged days add bids: a goal-only day hasn't been followed up.
    """
    return Counts(
        **{f: sum(getattr(c, f) for _, c, logged in days if logged) for f in BID_FIELDS},
        interviewsSelf=week_interviews["self"],
        interviewsCaller=week_interviews["caller"],
    )


async def weekly_plans_for(user_ids: list, monday: date) -> dict:
    """{userId: WeeklyPlan} for the week starting `monday` (migrated or not)."""
    start = _midnight(monday)
    plans = await WeeklyPlan.find({
        "userId": {"$in": user_ids},
        "$or": [
            {"weekStart": start},
            # Not migrated yet: matched by the stored local Monday midnight.
            {"weekStart": {"$exists": False},
             "startDate": {"$gte": start - timedelta(hours=12), "$lte": start + timedelta(hours=12)}},
        ],
    }).to_list()
    out: dict = {}
    for plan in sorted(plans, key=lambda p: p.updatedAt):
        out[plan.userId] = plan  # the most recently updated one wins
    return out


async def week_snapshot(user_ids: list, monday: date) -> dict:
    """Everything the week views need, for each user, in a handful of queries."""
    sunday = monday + timedelta(days=6)
    weekly = await weekly_plans_for(user_ids, monday)
    dailies = await DailyPlan.find({
        "userId": {"$in": user_ids},
        "date": {"$gte": _midnight(monday), "$lte": _midnight(sunday)},
    }).sort("date").to_list()
    interviews = await interview_counts(user_ids, monday, sunday)

    out: dict = {}
    for uid in user_ids:
        days = [p for p in dailies if p.userId == uid]
        day_auto = interviews.get(uid, {})
        by_date = {p.date.date(): p for p in days}
        day_done = {
            d: effective_daily_done(by_date.get(d), day_auto.get(d, empty_day()))
            for d in sorted(set(by_date) | set(day_auto))
        }
        logged = {d for d, p in by_date.items() if p.loggedAt is not None}
        week_interviews = {
            "self": sum(e["self"] for e in day_auto.values()),
            "caller": sum(e["caller"] for e in day_auto.values()),
        }
        out[uid] = {
            "plan": weekly.get(uid),
            "days": days,
            "dayDone": day_done,
            "dayAuto": day_auto,
            "done": weekly_done([(d, c, d in logged) for d, c in day_done.items()], week_interviews),
            "lastLogged": max(logged, default=None),
        }
    return out
