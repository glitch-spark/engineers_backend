"""Automatic Goal vs Done values for daily and weekly plans.

- Interviews Done comes from interview records: each countable round on a
  local day, split into self vs caller rounds (`split_rounds_by_day`).
- Profiles Done is the owner's Profiles (Accounts) counted per region.
- Weekly Done sums the daily flows (bids, interviews) and takes the latest
  logged day's stock levels (profiles, LinkedIn).

Nothing here writes to the database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rounds import load_interviews_for_window, split_rounds_by_day
from app.models import Account, DailyPlan, WeeklyPlan
from app.models.plan_common import PROFILE_REGIONS, Counts, WeeklyOverrides


def empty_day() -> dict:
    return {"self": 0, "caller": 0, "stages": {"self": {}, "caller": {}}}


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def interview_counts(user_ids: list, start: date, end: date) -> dict:
    """{userId: {local date: {"self", "caller", "stages": {"self": {...}, "caller": {...}}}}}."""
    tz = app_tz()
    start_utc = local_day_start_utc(start, tz)
    end_utc = local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    out: dict = {uid: {} for uid in user_ids}
    for iv in await load_interviews_for_window(user_ids, start_utc, end_utc):
        days = out.setdefault(iv.createdBy, {})
        for day, group, is_caller in split_rounds_by_day(iv, start_utc, end_utc, tz):
            who = "caller" if is_caller else "self"
            entry = days.setdefault(day, empty_day())
            entry[who] += 1
            entry["stages"][who][group] = entry["stages"][who].get(group, 0) + 1
    return out


async def profiles_by_region(user_ids: list) -> dict:
    """{userId: {region: count}} over each user's Profiles (Accounts)."""
    out: dict = {uid: {} for uid in user_ids}
    if not user_ids:
        return out
    rows = await Account.get_motor_collection().aggregate([
        {"$match": {"createdBy": {"$in": user_ids}}},
        {"$group": {"_id": {"user": "$createdBy", "region": "$region"}, "n": {"$sum": 1}}},
    ]).to_list(length=None)
    for row in rows:
        uid = row["_id"]["user"]
        region = row["_id"].get("region")
        if region not in PROFILE_REGIONS:
            region = "Unassigned"
        counts = out.setdefault(uid, {})
        counts[region] = counts.get(region, 0) + row["n"]
    return out


def effective_daily_done(plan: Optional[Any], auto_day: dict, auto_profiles: dict) -> Counts:
    """A day's Done with overrides applied (plan may be None for an empty day)."""
    if plan is None:
        return Counts(
            interviewsSelf=auto_day["self"],
            interviewsCaller=auto_day["caller"],
            profiles=auto_profiles,
        )
    overrides = plan.doneOverrides
    if overrides.profiles is not None:
        profiles = overrides.profiles
    elif plan.loggedAt is not None:
        profiles = plan.done.profiles
    else:
        profiles = auto_profiles
    return Counts(
        bidsSelf=plan.done.bidsSelf,
        bidsBidder=plan.done.bidsBidder,
        interviewsSelf=auto_day["self"] if overrides.interviewsSelf is None else overrides.interviewsSelf,
        interviewsCaller=auto_day["caller"] if overrides.interviewsCaller is None else overrides.interviewsCaller,
        profiles=profiles,
        linkedin=plan.done.linkedin,
    )


def weekly_done(days: list, week_interviews: dict, overrides: WeeklyOverrides) -> Counts:
    """Week Done from (date, daily Done, logged) tuples and the week's interview totals."""
    latest = max((d for d in days if d[2]), key=lambda d: d[0], default=None)
    done = Counts(
        bidsSelf=sum(c.bidsSelf for _, c, _ in days),
        bidsBidder=sum(c.bidsBidder for _, c, _ in days),
        interviewsSelf=week_interviews["self"],
        interviewsCaller=week_interviews["caller"],
        profiles=latest[1].profiles if latest else {},
        linkedin=latest[1].linkedin if latest else {},
    )
    for field, value in overrides.model_dump().items():
        if value is not None:
            setattr(done, field, value)
    return done


async def _weekly_plans(user_ids: list, monday: date) -> dict:
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
    weekly = await _weekly_plans(user_ids, monday)
    dailies = await DailyPlan.find({
        "userId": {"$in": user_ids},
        "date": {"$gte": _midnight(monday), "$lte": _midnight(sunday)},
    }).sort("date").to_list()
    interviews = await interview_counts(user_ids, monday, sunday)
    profiles = await profiles_by_region(user_ids)

    out: dict = {}
    for uid in user_ids:
        days = [p for p in dailies if p.userId == uid]
        day_auto = interviews.get(uid, {})
        user_profiles = profiles.get(uid, {})
        by_date = {p.date.date(): p for p in days}
        day_done = {
            d: effective_daily_done(by_date.get(d), day_auto.get(d, empty_day()), user_profiles)
            for d in sorted(set(by_date) | set(day_auto))
        }
        logged = {d for d, p in by_date.items() if p.loggedAt is not None}
        rows = [(d, c, d in logged) for d, c in day_done.items()]
        week_interviews = {
            "self": sum(c.interviewsSelf for c in day_done.values()),
            "caller": sum(c.interviewsCaller for c in day_done.values()),
        }
        plan = weekly.get(uid)
        overrides = plan.doneOverrides if plan else WeeklyOverrides()
        out[uid] = {
            "plan": plan,
            "days": days,
            "dayDone": day_done,
            "dayAuto": day_auto,
            "auto": weekly_done(rows, week_interviews, WeeklyOverrides()),
            "done": weekly_done(rows, week_interviews, overrides),
            "lastLogged": max(logged, default=None),
        }
    return out
