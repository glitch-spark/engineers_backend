"""`/dashboard` — headline "this week" card for the redesigned staff dashboard.

Pure computations (week windows, pace, streak, percentile) live in
`app.dashboard_stats` so they stay unit-testable without a database. This
router wires them to Mongo: daily-plan sums, the current weekly plan's
targets, and interview stage-tile counts (reusing `app.interview_rounds`,
the same grouping `app.routers.metrics` uses).
"""

from __future__ import annotations

import re
from math import ceil
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.dashboard_stats import (
    app_tz,
    bucket_for,
    bucket_keys,
    bucket_label,
    compare_window,
    local_day_start_utc,
    logging_ratio,
    month_keys,
    percent,
    percentile,
    plan_pace,
    pick_bucket,
    prorated_goal,
    rank_rows,
    streak,
    week_window,
)
from app.bidder_alerts import bidder_alerts
from app.deps import get_current_user
from app.interview_rounds import (
    BREAKDOWN_ORDER,
    count_interview_rounds,
    empty_breakdown,
    load_interviews_for_window,
)
from app.models import Account, DailyPlan, Interview, Transaction, UserExtra
from app.models.plan_common import monday_of
from app.plan_auto import (
    day_targets,
    goal_progress,
    interviews_by_day,
    interviews_split,
    WORKDAYS,
    routines_grid,
    routines_tally,
    weekly_plans_for,
    weeks_or_drafts,
)
from app.profile_alerts import profile_alerts
from app.profile_metrics import ProfileSpan
from app.routers.accounts import archive_status_query
from app.routers.interviews import follow_ups_summary, needs_items
from app.routers._plan_access import read_subject
from app.routers._util import to_object_id
from app.routers.metrics import _display_name
from app.routers.transactions import _TOTALS_GROUP
from app.stats import interview_report
from app.stats.conversion_scope import DATA_START
from app.stats.profile_loader import cached, load_interview_facts

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class _IdOnly(BaseModel):
    id: PydanticObjectId = Field(alias="_id")


def _midnight(d: date) -> datetime:
    """DailyPlan.date is naive midnight of the calendar day — no tz math."""
    return datetime(d.year, d.month, d.day)


async def _subject_id(user: UserExtra, user_id: Optional[str]):
    """Resolve which user's data to show. Absent -> caller. Present:
    admin -> that user (404 if unknown), non-admin -> 403."""
    if not user_id:
        return user.id
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    oid = to_object_id(user_id)
    target = await UserExtra.get(oid)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return oid


_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_ymd(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    if not _YMD_RE.match(value):
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    if not (2000 <= parsed.year <= 2100):
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    return parsed


def _today(value: Optional[str]) -> date:
    parsed = _parse_ymd(value, "today")
    return parsed if parsed is not None else datetime.now(app_tz()).date()


# Logged plans, plus documents from before Goal vs Done (no `loggedAt` field
# until scripts/migrate_plans_goal_done.py runs; the model folds them on read).
_LOGGED_OR_LEGACY = {"$or": [{"loggedAt": {"$ne": None}}, {"loggedAt": {"$exists": False}}]}


async def _logged_plans(user_ids: list, start: date, end: date) -> list[DailyPlan]:
    """Daily plans whose Done was logged (goal-only entries don't count)."""
    plans = await DailyPlan.find(
        {
            "userId": {"$in": user_ids},
            "date": {"$gte": _midnight(start), "$lte": _midnight(end)},
            **_LOGGED_OR_LEGACY,
        }
    ).to_list()
    return [p for p in plans if p.loggedAt is not None]


def _now() -> datetime:
    """Naive UTC now (the interview outcome rules' clock)."""
    return datetime.utcnow()


def _target(total: int) -> Optional[int]:
    return total or None


_TICK = timedelta(microseconds=1)


def _week_bounds(monday: date, tz) -> tuple[datetime, datetime]:
    """Naive-UTC [start, end) of the Mon-Sun week in the app timezone."""
    return local_day_start_utc(monday, tz), local_day_start_utc(monday + timedelta(days=7), tz)


def _stage_counts(ivs: list, start: datetime, end: datetime) -> tuple[dict, int]:
    """(rounds per stage tile, canceled rounds) of the rounds dated in [start, end]."""
    breakdown = empty_breakdown()
    canceled_total = 0
    for iv in ivs:
        _rounds, canceled, bd = count_interview_rounds(iv, start, end)
        canceled_total += canceled
        for k, v in bd.items():
            breakdown[k] = breakdown.get(k, 0) + v
    return breakdown, canceled_total


@router.get("/week")
async def dashboard_week(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
    today: Optional[str] = Query(None),
    weekOf: Annotated[Optional[str], Query()] = None,
) -> dict:
    """`weekOf` (any date in it; default the week of `today`) picks the Mon-Sun week reported, never a later one
    than today's. `today` stays the real date: elapsed days, pace and the routines' day states are judged against
    it, so a past week is all elapsed (pace: everything expected, `perDayNeeded` null). `userId`: anyone's week,
    for any active user (Home is readable by anyone, read-only)."""
    sid = await read_subject(user, userId)
    day = _today(today)
    tz = app_tz()
    monday, saturday, sunday = week_window(_parse_ymd(weekOf, "weekOf") or day)
    if monday > day:
        raise HTTPException(status_code=422, detail="invalid weekOf")

    # Actuals: every stored daily plan over the counting window (Mon-Sun), drafts included;
    # interviews come from interview records.
    plans = await DailyPlan.find(
        {"userId": sid, "date": {"$gte": _midnight(monday), "$lte": _midnight(sunday)}}
    ).to_list()
    plans_by_day = {p.date.date(): p for p in plans}
    self_sum = sum(p.done.bidsSelf for p in plans)
    bidder_sum = sum(p.done.bidsBidder for p in plans)
    bids_total = self_sum + bidder_sum

    # This week's weekly plan Goal is the target (0 means none set).
    weekly_plan = (await weekly_plans_for([sid], monday)).get(sid)
    workdays = [monday + timedelta(days=i) for i in range(WORKDAYS)]
    elapsed = [d for d in workdays if d <= day]
    days_left = len(workdays) - len(elapsed)
    targets = {d: day_targets(plans_by_day.get(d), weekly_plan, d) for d in workdays}
    day_target = {d: t.bidsSelf + t.bidsBidder for d, t in targets.items()}
    # Mon-Sat sums from the sources `byDay` uses (Sunday's bids are in `total` only)
    week_self = sum(p.done.bidsSelf for d in workdays if (p := plans_by_day.get(d)))
    week_bidder = sum(p.done.bidsBidder for d in workdays if (p := plans_by_day.get(d)))
    bids_expected = sum(day_target[d] for d in elapsed)
    routines_done, routines_expected = routines_tally(weekly_plan, plans_by_day, elapsed) if weekly_plan else (0, 0)
    weekly_goals = []
    if weekly_plan:
        progress = goal_progress(weekly_plan, plans)
        weekly_goals = [
            {"id": g.id, "text": g.text, "kind": g.kind, **progress[g.id]} for g in weekly_plan.weeklyGoals if not g.archived
        ]
    goal = weekly_plan.goal if weekly_plan else None
    bids_target = _target(goal.bidsSelf + goal.bidsBidder) if goal else None
    interviews_target = _target(goal.interviewsSelf + goal.interviewsCaller) if goal else None

    # Stage tiles: interview rounds created by the user, dated in the window.
    iv_start, iv_end = _week_bounds(monday, tz)
    week_ivs = await load_interviews_for_window([sid], iv_start, iv_end - _TICK)
    breakdown, canceled_total = _stage_counts(week_ivs, iv_start, iv_end - _TICK)
    stages = [{"key": key, "label": label, "count": breakdown.get(key, 0)} for key, label in BREAKDOWN_ORDER]
    stages.append({"key": "canceled", "label": "Canceled", "count": canceled_total})
    rounds = interview_report.own_rows(week_ivs, iv_start, iv_end, _now(), tz)
    outcomes = interview_report.counts(rounds)
    rounds_by_day = {d: interview_report.counts([r for r in rounds if r["local"].date() == d]) for d in workdays}
    companies = {iv.id: iv.companyName for iv in week_ivs}
    # Home's week table: each working-day round with where it stands (a waiting round that was followed up says so)
    round_list = [
        {"date": r["local"].date().isoformat(), "interviewId": str(r["iid"]), "companyName": companies.get(r["iid"]),
         "stage": r["stage"], "at": r["at"].isoformat() + "Z", "who": "caller" if r["byCaller"] else "self",
         "callerName": r["caller"][1] if r["byCaller"] and r["caller"] else None,
         "status": "followed_up" if r["outcome"] == "waiting" and r["followed"] else r["outcome"]}
        for r in sorted(rounds, key=lambda r: r["at"]) if r["local"].date() in rounds_by_day
    ]

    # Streak: consecutive working days (Mon-Sat) with a logged plan, looking
    # back up to 400 days (the helper itself caps the walk at 365).
    streak_docs = await _logged_plans([sid], day - timedelta(days=400), day)
    plan_days = {p.date.date() for p in streak_docs}
    streak_value = streak(plan_days, day)

    # Percentile population: non-admin users that still exist, with >=1
    # logged plan in the last 30 days (app timezone), always including the
    # subject. Intersecting with existing, non-admin users (rather than just
    # subtracting admins) drops plans left behind by deleted users, so they
    # no longer count toward N.
    population_start = day - timedelta(days=29)
    active_ids = await DailyPlan.get_motor_collection().distinct(
        "userId",
        {"date": {"$gte": _midnight(population_start), "$lte": _midnight(day)}, **_LOGGED_OR_LEGACY},
    )
    eligible = await UserExtra.find(
        {"_id": {"$in": active_ids}, "role": {"$ne": "admin"}},
        projection_model=_IdOnly,
    ).to_list()
    population = {u.id for u in eligible}
    population.add(sid)

    members = list(population)
    score_docs = await _logged_plans(members, monday, sunday)
    bids_scores = {uid: 0 for uid in population}
    for p in score_docs:
        bids_scores[p.userId] = bids_scores.get(p.userId, 0) + p.done.bidsSelf + p.done.bidsBidder
    interviews = await interviews_split(members, monday, sunday)
    iv_scores = {uid: interviews[uid]["self"] + interviews[uid]["caller"] for uid in population}
    interviews_done = iv_scores[sid]

    return {
        "week": {"start": monday.isoformat(), "end": saturday.isoformat()},
        "bids": {
            "self": self_sum,
            "bidder": bidder_sum,
            "total": bids_total,
            "target": bids_target,
            "pace": plan_pace(bids_total, bids_target, bids_expected, days_left),
            "percentile": percentile(bids_scores[sid], list(bids_scores.values())),
            "byDay": [
                {"date": d.isoformat(), "done": (p.done.bidsSelf + p.done.bidsBidder) if (p := plans_by_day.get(d)) else 0,
                 "self": p.done.bidsSelf if p else 0, "bidder": p.done.bidsBidder if p else 0,
                 "target": _target(day_target[d])}
                for d in workdays
            ],
            "week": {
                "self": week_self, "selfTarget": _target(sum(t.bidsSelf for t in targets.values())),
                "bidder": week_bidder, "bidderTarget": _target(sum(t.bidsBidder for t in targets.values())),
                "total": week_self + week_bidder, "target": _target(sum(day_target.values())),
            },
        },
        "interviews": {
            "done": interviews_done,
            "self": interviews[sid]["self"],
            "caller": interviews[sid]["caller"],
            "target": interviews_target,
            "pace": plan_pace(interviews_done, interviews_target, ceil((interviews_target or 0) * len(elapsed) / WORKDAYS), days_left),
            "percentile": percentile(interviews_done, list(iv_scores.values())),
            # rounds scheduled Mon-Sun by the Statistics → Interviews outcome rule; scheduled = not held yet
            "outcomes": {k: outcomes[k] for k in ("held", "passed", "rejected", "noAnswer", "waiting", "canceled",
                                                  "scheduled")},
            # the same rounds per working day they are scheduled on (a Sunday round is in `outcomes` only)
            "byDay": [{"date": d.isoformat(), **{k: rounds_by_day[d][k] for k in ("held", "canceled", "scheduled")}}
                      for d in workdays],
            "rounds": round_list,
        },
        "stages": stages,
        "streak": streak_value,
        "hasWeeklyPlan": weekly_plan is not None,
        "routines": {"done": routines_done, "expected": routines_expected,
                     "rows": routines_grid(weekly_plan, plans_by_day, workdays, day) if weekly_plan else []},
        "weeklyGoals": weekly_goals,
    }


_MAX_RANGE_DAYS = 1096


@router.get("/activity")
async def dashboard_activity(
    user: Annotated[UserExtra, Depends(get_current_user)],
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
) -> dict:
    sid = await _subject_id(user, userId)
    start = _parse_ymd(from_, "from")
    end = _parse_ymd(to, "to")
    if start is None or end is None or start > end or (end - start).days + 1 > _MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail="invalid range")

    bucket = pick_bucket(start, end)
    keys = bucket_keys(start, end, bucket)
    series = {
        key: {
            "key": key.isoformat(),
            "label": bucket_label(key, bucket, start, end),
            "self": 0,
            "bidder": 0,
            "interviews": 0,
        }
        for key in keys
    }

    for p in await _logged_plans([sid], start, end):
        row = series[bucket_for(p.date.date(), bucket, keys)]
        row["self"] += p.done.bidsSelf
        row["bidder"] += p.done.bidsBidder
    for day, count in (await interviews_by_day([sid], start, end)).get(sid, {}).items():
        series[bucket_for(day, bucket, keys)]["interviews"] += count

    series_list = [series[key] for key in keys]
    totals = {
        "self": sum(row["self"] for row in series_list),
        "bidder": sum(row["bidder"] for row in series_list),
        "bids": sum(row["self"] + row["bidder"] for row in series_list),
        "interviews": sum(row["interviews"] for row in series_list),
    }

    return {
        "bucket": bucket,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "series": series_list,
        "totals": totals,
    }


@router.get("/net-monthly")
async def dashboard_net_monthly(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
    today: Optional[str] = Query(None),
) -> dict:
    # userId=all (admins): everyone's transactions summed (the Transactions page on "All users").
    if userId == "all":
        if user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
        who: dict = {}
    else:
        who = {"userId": await _subject_id(user, userId)}
    day = _today(today)  # app-timezone "today" — decides which 12 months.

    keys = month_keys(day, 12)
    first_year, first_month = (int(part) for part in keys[0].split("-"))
    first_of_oldest_month = date(first_year, first_month, 1)
    next_month_year, next_month = day.year, day.month + 1
    if next_month > 12:
        next_month = 1
        next_month_year += 1
    first_of_next_month = date(next_month_year, next_month, 1)

    # Transaction.date is a calendar date stored as naive midnight (see
    # POST /transactions + /transactions/summary) — NOT a tz-aware instant.
    # Compare and group it as-is, with no app-timezone conversion, or a
    # transaction dated the 1st of a month drifts into the previous month's
    # bucket (or out of the window entirely) once APP_TIMEZONE != UTC.
    start = _midnight(first_of_oldest_month)
    end = _midnight(first_of_next_month)

    pipeline = [
        {
            "$match": {
                **who,
                "status": "approved",
                "date": {"$gte": start, "$lt": end},
            }
        },
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m", "date": "$date"}},
                **_TOTALS_GROUP,
            }
        },
    ]
    rows = await Transaction.aggregate(pipeline).to_list()
    by_period = {row["_id"]: row for row in rows}

    months = []
    total = 0.0
    for period in keys:
        row = by_period.get(period)
        income = round(float(row.get("income") or 0), 2) if row else 0.0
        outcome = round(float(row.get("outcome") or 0), 2) if row else 0.0
        net = round(income - outcome, 2)
        months.append({"period": period, "income": income, "outcome": outcome, "net": net,
                       "in": income, "out": outcome})  # the Home card's names for income / outcome
        total += net

    return {"months": months, "total": round(total, 2)}


@router.get("/month-interviews")
async def dashboard_month_interviews(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
    today: Optional[str] = Query(None),
) -> dict:
    """Home's "This month" interview tiles (spec 2026-11-20-home-redesign-design.md), the subject's own numbers:
    `pipeline`, this week's stage tiles (`/week`'s `stages` without Canceled); `waitingForResult`, the rounds since
    the data start that are the interview's latest, held and without a pass / reject; `followUpsDue`, the standup's
    `followUps.toDo`; `passRate` over the rounds scheduled this month (app timezone) by the Statistics → Interviews
    rules, beside the whole team's (`teamRate`, from that report's cached load)."""
    sid = await _subject_id(user, userId)
    day = _today(today)
    tz, now = app_tz(), _now()
    cutoff = local_day_start_utc(DATA_START, tz)
    ivs = await Interview.find({"createdBy": sid, "$or": [
        {"scheduledAt": {"$gte": cutoff}}, {"stageHistory.scheduledAt": {"$gte": cutoff}},
    ]}).to_list()
    rows = interview_report.own_rows(ivs, cutoff, datetime.max, now, tz)

    week_start, week_end = _week_bounds(week_window(day)[0], tz)
    breakdown, _canceled = _stage_counts(ivs, week_start, week_end - _TICK)
    month_start = max(local_day_start_utc(day.replace(day=1), tz), cutoff)
    month_end = local_day_start_utc((day.replace(day=1) + timedelta(days=32)).replace(day=1), tz)
    mine = interview_report.counts([r for r in rows if month_start <= r["at"] < month_end])
    team = await load_interview_facts(month_start, month_end) if month_end > cutoff else []
    team_rate = interview_report.pass_rate(
        interview_report.period_rows(team, {}, month_start, month_end, now, tz, deleted=True))
    return {
        "pipeline": [{"stage": key, "label": label, "count": breakdown.get(key, 0)} for key, label in BREAKDOWN_ORDER],
        "waitingForResult": sum(
            1 for r in rows if r["outcome"] == "waiting" or (r["latest"] and r["status"] == "completed")),
        "followUpsDue": (await follow_ups_summary(sid, day))["toDo"],
        "passRate": {"value": mine["rate"], "passed": mine["passed"], "decided": mine["decided"], "few": mine["few"],
                     "teamRate": round(team_rate, 1) if team_rate is not None else None},
    }


async def _active_members() -> list[UserExtra]:
    """Everyone the team views rank: active, non-admin users."""
    return await UserExtra.find({"role": {"$ne": "admin"}, "isActive": {"$ne": False}}).to_list()


async def team_metrics(
    members: list[UserExtra], start: date, end: date, today: date, *, with_streak: bool = False,
) -> dict:
    """Every dashboard metric per member over [start, end]: bids (self/bidder), interviews (self/caller), net earnings, goal
    attainment (weekly goals pro-rated to the period), plan logging, and — with `with_streak` — the current streak
    (a 400-day plan scan, so only the team table asks for it)."""
    ids = [m.id for m in members]
    out = {uid: {"bids": 0, "bidsSelf": 0, "bidsBidder": 0, "interviews": 0, "interviewsSelf": 0, "interviewsCaller": 0,
                 "earnings": 0.0} for uid in ids}

    for p in await _logged_plans(ids, start, end):
        row = out[p.userId]
        row["bidsSelf"] += p.done.bidsSelf
        row["bidsBidder"] += p.done.bidsBidder
        row["bids"] += p.done.bidsSelf + p.done.bidsBidder
    split = await interviews_split(ids, start, end)
    for uid in ids:
        row = out[uid]
        row["interviewsSelf"], row["interviewsCaller"] = split[uid]["self"], split[uid]["caller"]
        row["interviews"] = row["interviewsSelf"] + row["interviewsCaller"]
    # Transaction.date is a naive calendar midnight (see /net-monthly): compare it as-is.
    rows = await Transaction.aggregate([
        {"$match": {"userId": {"$in": ids}, "status": "approved",
                    "date": {"$gte": _midnight(start), "$lt": _midnight(end + timedelta(days=1))}}},
        {"$group": {"_id": "$userId", "net": {"$sum": "$amount"}}},
    ]).to_list()
    for row in rows:
        out[row["_id"]]["earnings"] = round(float(row["net"]), 2)

    bid_goals: dict = {uid: {} for uid in ids}
    iv_goals: dict = {uid: {} for uid in ids}
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        for uid, wp in (await weekly_plans_for(ids, monday)).items():
            bid_goals[uid][monday] = wp.goal.bidsSelf + wp.goal.bidsBidder
            iv_goals[uid][monday] = wp.goal.interviewsSelf + wp.goal.interviewsCaller
        monday += timedelta(days=7)

    logged_days: dict = {uid: set() for uid in ids}
    for p in await _logged_plans(ids, start, min(end, today)):
        logged_days[p.userId].add(p.date.date())
    streak_days: dict = {uid: set() for uid in ids}
    if with_streak:
        for p in await _logged_plans(ids, today - timedelta(days=400), today):
            streak_days[p.userId].add(p.date.date())

    for uid in ids:
        row = out[uid]
        row["goalBids"] = prorated_goal(bid_goals[uid], start, end)
        row["goalInterviews"] = prorated_goal(iv_goals[uid], start, end)
        row["goalBidsPct"] = percent(row["bids"], row["goalBids"])
        row["goalInterviewsPct"] = percent(row["interviews"], row["goalInterviews"])
        row["logged"], row["workingDays"] = logging_ratio(logged_days[uid], start, end, today)
        if with_streak:
            row["streak"] = streak(streak_days[uid], today)
    return out


@router.get("/compare")
async def dashboard_compare(
    user: Annotated[UserExtra, Depends(get_current_user)],
    range_: str = Query("week", alias="range"),
    userId: Optional[str] = Query(None),
    today: Optional[str] = Query(None),
) -> dict:
    """"You vs team": bids, interviews and earnings ranked across active non-admin users over the period.

    The subject (the caller, or the user an admin picked) is reported separately; an admin viewing nobody has
    no subject rank. All numbers come from `team_metrics`, the same as the admin team table."""
    sid = await _subject_id(user, userId)
    day = _today(today)
    try:
        start, end = compare_window(day, range_)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid range")

    members = await _active_members()
    people = {m.id: m for m in members}
    stats = await team_metrics(members, start, end, day)

    def ranked(key: str, split: Optional[dict] = None) -> dict:
        """`split`: {row field: stats key} added to each row, e.g. interviews' self and caller parts."""
        board = rank_rows([(uid, _display_name(people[uid]), row[key]) for uid, row in stats.items()])
        mine = next((r for r in board if r["id"] == sid), None)
        return {
            "rows": [
                {"userId": str(r["id"]), "name": r["name"], "image": people[r["id"]].image or None,
                 "value": r["value"], "rank": r["rank"],
                 **{field: stats[r["id"]][k] for field, k in (split or {}).items()}}
                for r in board
            ],
            "subject": {"rank": mine["rank"] if mine else None, "value": mine["value"] if mine else None,
                        "outOf": len(board)},
        }

    return {
        "range": range_,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "subjectId": str(sid),
        "metrics": {
            "bids": ranked("bids"),
            "interviews": ranked("interviews", {"self": "interviewsSelf", "caller": "interviewsCaller"}),
            "earnings": ranked("earnings"),
        },
    }


def _team_goal_pct(stats: dict, done_key: str, goal_key: str) -> Optional[int]:
    """Team done ÷ team goal over the members who have a goal (no goal is left out, not counted as 0%)."""
    with_goal = [row for row in stats.values() if row[goal_key]]
    return percent(sum(row[done_key] for row in with_goal), sum(row[goal_key] for row in with_goal))


@router.get("/team")
async def dashboard_team(
    user: Annotated[UserExtra, Depends(get_current_user)],
    range_: str = Query("week", alias="range"),
    today: Optional[str] = Query(None),
) -> dict:
    """Admins' "All users" view: team totals and one row per active non-admin user, by bids (high first)."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    day = _today(today)
    try:
        start, end = compare_window(day, range_)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid range")

    members = await _active_members()
    stats = await team_metrics(members, start, end, day, with_streak=True)
    users = [
        # Admins manage these people, so real names here (the leaderboard's anonymity applies to /compare).
        {"userId": str(m.id), "name": (m.name or m.email or "").strip() or _display_name(m), "image": m.image or None,
         **{k: stats[m.id][k] for k in ("bids", "bidsSelf", "bidsBidder", "interviews", "interviewsSelf",
                                        "interviewsCaller", "earnings", "goalBidsPct", "goalInterviewsPct", "logged",
                                        "workingDays", "streak")}}
        for m in members
    ]
    users.sort(key=lambda u: (-u["bids"], u["name"].lower()))
    rows = stats.values()
    return {
        "range": range_,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": {
            "bids": sum(r["bids"] for r in rows),
            "bidsSelf": sum(r["bidsSelf"] for r in rows),
            "bidsBidder": sum(r["bidsBidder"] for r in rows),
            "interviews": sum(r["interviews"] for r in rows),
            "interviewsSelf": sum(r["interviewsSelf"] for r in rows),
            "interviewsCaller": sum(r["interviewsCaller"] for r in rows),
            "earnings": round(sum(r["earnings"] for r in rows), 2),
            "goalBidsPct": _team_goal_pct(stats, "bids", "goalBids"),
            "goalInterviewsPct": _team_goal_pct(stats, "interviews", "goalInterviews"),
            "logged": sum(r["logged"] for r in rows),
            "workingDays": sum(r["workingDays"] for r in rows),
        },
        "users": users,
    }


def _local(now: datetime) -> datetime:
    """A naive UTC time in the app timezone."""
    return now.replace(tzinfo=timezone.utc).astimezone(app_tz())


async def _flags(ids: list, today: date) -> dict:
    """{userId: {"profiles", "bidders"}}: the Profiles page banners of each person's live profiles
    (`app.profile_alerts`) and their bidders' flags (`app.bidder_alerts`), in a fixed number of reads for them all."""
    out: dict = {uid: {"profiles": [], "bidders": []} for uid in ids}
    accounts = await Account.find(
        {**archive_status_query("active"), "createdBy": {"$in": ids}}).project(ProfileSpan).to_list()
    owner = {str(a.id): a.createdBy for a in accounts}
    alerts = await profile_alerts(accounts, today)
    for kind, key in (("no_interviews", "noInterviews"), ("no_bids", "noApplies")):
        for a in alerts[key]:
            out[owner[a["accountId"]]]["profiles"].append({
                "accountId": a["accountId"], "name": a["name"], "kind": kind, "idleDays": a["idleDays"],
                "capped": a["capped"], "bids": a.get("bids"), "tailorScore": (a.get("tailorScore") or {}).get("avg")})
    for uid, rows in (await bidder_alerts(ids, today)).items():
        out[uid]["bidders"] = rows
    return out


FLAGS_TTL = 600  # seconds the team list keeps the profile and bidder flags: they move by the day, and reading them is
# the slow part of the list (half a year of bids per profile), so ten minutes spares most loads that cost


async def _pending(ids: list, now: datetime, *, cache: bool = False) -> dict:
    """{userId: the `/dashboard/pending` body}: what waits on each person at `now` (naive UTC)."""
    today = _local(now).date()
    if cache:
        flags = await cached(("team-flags", today, tuple(sorted(ids))), lambda: _flags(ids, today), FLAGS_TTL)
    else:
        flags = await _flags(ids, today)
    rounds: dict = {str(uid): [] for uid in ids}
    for item in await needs_items(ids, now):
        rounds[item["ownerId"]].append(item)
    out = {}
    for uid in ids:
        groups = {"interviews": rounds[str(uid)], **flags[uid]}
        counts = {k: len(v) for k, v in groups.items()}
        out[uid] = {**groups, "counts": {**counts, "total": sum(counts.values())}}
    return out


@router.get("/pending")
async def dashboard_pending(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    """Pending items (spec 2026-11-21-admin-home-design.md), the subject's own: `interviews`, their needs-input
    rows; `profiles`, the Profiles page banners (`no_interviews` / `no_bids`); `bidders`, `not_bidding` / `slowing`
    (`app.bidder_alerts`); `counts` per group and their `total`. `userId`: anyone's, for any active user (Home)."""
    sid = await read_subject(user, userId)
    return (await _pending([sid], _now()))[sid]


_STATE_ORDER = {"red": 0, "amber": 1, "green": 2}


@router.get("/team-today")
async def dashboard_team_today(
    user: Annotated[UserExtra, Depends(get_current_user)],
    day: Optional[str] = Query(None),
) -> dict:
    """The admin Home's list of people (spec 2026-11-21-admin-home-design.md), one row per active non-admin user for
    `day` (default today): `bids`, the day view's done and target (null without one); `todos`, the day's routines
    done / shown; `pending`, their `/dashboard/pending` total right now; `state`: `red` with a round that ended and
    is not marked or any profile / bidder flag, else `amber` when the day's bids are under target (today: only from
    midday in the app timezone) or a routine of the day is not done, else `green`. Red first, then amber, then
    green; inside a colour most pending first, then by name. A fixed number of reads for the whole team; the flags
    are cached for FLAGS_TTL."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    now = _now()
    local = _local(now)
    when = _parse_ymd(day, "day") or local.date()
    bids_count = when < local.date() or (when == local.date() and local.hour >= 12)
    members = await _active_members()
    ids = [m.id for m in members]
    weekly = await weeks_or_drafts(ids, monday_of(when))
    plans = {p.userId: p for p in await DailyPlan.find({"userId": {"$in": ids}, "date": _midnight(when)}).to_list()}
    pending = await _pending(ids, now, cache=True) if ids else {}
    people = []
    for m in members:
        plan, week, waiting = plans.get(m.id), weekly.get(m.id), pending[m.id]
        target = day_targets(plan, week, when)
        target = _target(target.bidsSelf + target.bidsBidder)
        done = (plan.done.bidsSelf + plan.done.bidsBidder) if plan else 0
        todos_done, todos = routines_tally(week, {when: plan}, [when]) if week else (0, 0)
        # red = an interview ended and nobody marked it. Profile and bidder flags only add to `pending`: on real
        # data everyone has a profile without an interview in two weeks, so they would paint the whole list red.
        if any(i["need"] == "outcome" for i in waiting["interviews"]):
            state = "red"
        elif (bids_count and target is not None and done < target) or todos_done < todos:
            state = "amber"
        else:
            state = "green"
        people.append({
            "userId": str(m.id), "name": (m.name or m.email or "").strip() or _display_name(m),
            "bids": {"done": done, "target": target}, "todos": {"done": todos_done, "total": todos},
            "pending": waiting["counts"]["total"], "state": state,
        })
    people.sort(key=lambda p: (_STATE_ORDER[p["state"]], -p["pending"], p["name"].lower()))
    return {"day": when.isoformat(), "people": people}
