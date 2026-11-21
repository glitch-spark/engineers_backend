"""Automatic Goal vs Done values for daily and weekly plans.

- Interviews Done always comes from interview records: each round held on a
  local day, split into self vs caller rounds (`split_rounds_by_day`).
- A day's Done is its typed bids plus those interviews.
- A week's Done sums the bids of its logged days and its interviews.

Nothing here writes to the database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil
from typing import Any, Optional

from app.alerts import app_week_key
from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rounds import load_interviews_for_window, split_rounds_by_day
from app.models import DailyPlan, WeeklyPlan
from app.models.plan_common import BID_FIELDS, Counts, DoneCounts, bids_of, result_is_done


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


async def interviews_split(user_ids: list, start: date, end: date) -> dict:
    """{userId: {"self": rounds the owner took, "caller": caller rounds}} held over [start, end]."""
    counts = await interview_counts(user_ids, start, end)
    return {
        uid: {who: sum(e[who] for e in counts.get(uid, {}).values()) for who in ("self", "caller")}
        for uid in user_ids
    }


def effective_daily_done(plan: Optional[Any], auto_day: dict) -> DoneCounts:
    """A day's Done: typed bids, also per profile (when there is a plan), and its interviews."""
    return DoneCounts(
        **(bids_of(plan.done) if plan else {}),
        bidsSelfByProfile=plan.done.bidsSelfByProfile if plan else {},
        bidsSelfOtherByProfile=(getattr(plan.done, "bidsSelfOtherByProfile", None) or {}) if plan else {},
        interviewsSelf=auto_day["self"],
        interviewsCaller=auto_day["caller"],
    )


def day_targets(plan: Optional[Any], weekly: Optional[Any], day: date) -> Counts:
    """A day's bid target: the day's own override, else the week's goal split over Mon-Fri (interviews 0)."""
    if plan is not None and plan.targetOverride is not None:
        return plan.targetOverride
    if weekly is None or day.weekday() >= 5:
        return Counts()
    return Counts(**{f: ceil(getattr(weekly.goal, f) / 5) for f in BID_FIELDS})


def day_items(weekly: Optional[Any], plan: Optional[Any], day: date) -> list[dict]:
    """{item, scope, result} lines for a day: live routines (Mon-Fri) and weekly goals, plus archived
    ones the day has a result for. A routine's frozen result target replaces the item's."""
    if weekly is None:
        return []
    results = plan.results if plan is not None else {}
    out = []
    for scope, items in (("routine", weekly.routines), ("goal", weekly.weeklyGoals)):
        for item in items:
            result = results.get(item.id)
            if item.archived and result is None:
                continue
            if scope == "routine" and day.weekday() >= 5 and result is None:
                continue
            if scope == "routine" and result is not None and result.target is not None:
                item = item.model_copy(update={"target": result.target})
            out.append({"item": item, "scope": scope, "result": result})
    return out


def _routine_done(item: Any, r: Optional[Any]) -> bool:
    """A routine's day is done: ticked, or its value at / over the target (the result's frozen one, else the item's)."""
    return result_is_done(item.kind, r, r.target if r is not None and r.target is not None else item.target)


def items_tally(weekly: Optional[Any], plan: Optional[Any], day: date) -> tuple[int, int]:
    """(done, total) over the day's routines with a result (done = ticked, or value at / over its target) and its
    weekly goals touched that day like the standup post lists them (in focus, ticked or a value; done = either)."""
    done = total = 0
    focus = set(plan.focus) if plan is not None else set()
    for e in day_items(weekly, plan, day):
        r = e["result"]
        if e["scope"] == "goal":
            hit = r is not None and (bool(r.entries) if e["item"].kind == "log" else (r.done or (r.value or 0) > 0))
            if hit or e["item"].id in focus:
                total += 1
                done += hit
            continue
        if r is None:
            continue
        total += 1
        done += _routine_done(e["item"], r)
    return done, total


def routines_tally(weekly: Any, plans: dict, days: list) -> tuple[int, int]:
    """(done, expected) routine lines over `days`: every routine shown on a day expects one tick or value."""
    done = expected = 0
    for d in days:
        for e in day_items(weekly, plans.get(d), d):
            if e["scope"] != "routine":
                continue
            expected += 1
            done += _routine_done(e["item"], e["result"])
    return done, expected


def routines_grid(weekly: Any, plans: dict, days: list, today: date) -> list[dict]:
    """One row per routine shown on any of `days`, with each day's state: `done`, `short` (a value under its target,
    carried as `value` / `target`), `missed` (a past day with nothing), `today` (today, nothing yet), `future`, or
    `off` (not shown that day: archived with no result). `doneDays` / `totalDays` count the days up to `today` it
    was shown, so their sums are `routines_tally` over those days."""
    shown = {d: {e["item"].id: e for e in day_items(weekly, plans.get(d), d) if e["scope"] == "routine"} for d in days}
    rows = []
    for routine in weekly.routines:
        cells = []
        for d in days:
            e = shown[d].get(routine.id)
            value = target = None
            if e is None:
                state = "off"
            elif d > today:
                state = "future"
            elif _routine_done(e["item"], e["result"]):
                state = "done"
            elif e["result"] is not None and e["result"].value and e["item"].target is not None:
                state, value, target = "short", e["result"].value, e["item"].target
            else:
                state = "missed" if d < today else "today"
            cells.append({"date": d.isoformat(), "state": state, "value": value, "target": target})
        counted = [c["state"] for c in cells if c["state"] not in ("off", "future")]
        if any(c["state"] != "off" for c in cells):
            rows.append({"id": routine.id, "text": routine.text, "days": cells,
                         "doneDays": counted.count("done"), "totalDays": len(counted)})
    return rows


def goal_progress(weekly: Any, days: list) -> dict[str, dict]:
    """Per weekly goal id: summed values, done (a day ticked it or the week total reached the target), doneOn (earliest done day) and target."""
    out = {}
    for g in weekly.weeklyGoals:
        rs = [(d, r) for d in days if (r := d.results.get(g.id)) is not None]
        if g.kind == "log":
            done_days = sorted(str(d.date)[:10] for d, r in rs if r.entries)
            value = sum(len(r.entries) for _, r in rs)
            out[g.id] = {"value": value, "done": value > 0, "target": g.target,
                         "doneOn": done_days[0] if done_days else None}
            continue
        value = sum(r.value or 0 for _, r in rs)
        done_days = sorted(str(d.date)[:10] for d, r in rs if r.done)
        out[g.id] = {"value": value, "done": bool(done_days) or bool(g.target and value >= g.target),
                     "target": g.target, "doneOn": done_days[0] if done_days else None}
    return out


def _draft(prev: WeeklyPlan, monday: date) -> WeeklyPlan:
    start = _midnight(monday)
    year, week_number, _key = app_week_key(start)
    return WeeklyPlan(
        userId=prev.userId, weekStart=start, startDate=start,
        endDate=start + timedelta(days=7) - timedelta(milliseconds=1), weekNumber=week_number, year=year,
        goal=prev.goal.model_copy(), routines=[r.model_copy() for r in prev.routines if not r.archived],
    )


async def draft_week(uid, monday: date) -> Optional[WeeklyPlan]:
    """Unsaved plan for `monday` carrying over last week's goal and live routines (no weekly goals)."""
    prev = (await weekly_plans_for([uid], monday - timedelta(days=7))).get(uid)
    return _draft(prev, monday) if prev is not None else None


async def week_or_draft(uid, monday: date) -> Optional[WeeklyPlan]:
    """The saved plan for `monday`, else the carry-over draft (None without either)."""
    return (await weekly_plans_for([uid], monday)).get(uid) or await draft_week(uid, monday)


async def weeks_or_drafts(user_ids: list, monday: date) -> dict:
    """{userId: `week_or_draft`} for many users in two queries (users with neither are left out)."""
    out = await weekly_plans_for(user_ids, monday)
    missing = [uid for uid in user_ids if uid not in out]
    if missing:
        prev = await weekly_plans_for(missing, monday - timedelta(days=7))
        out.update({uid: _draft(p, monday) for uid, p in prev.items()})
    return out


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
