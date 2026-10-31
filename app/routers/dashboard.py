"""`/dashboard` — headline "this week" card for the redesigned staff dashboard.

Pure computations (week windows, pace, streak, percentile) live in
`app.dashboard_stats` so they stay unit-testable without a database. This
router wires them to Mongo: daily-plan sums, the current weekly plan's
targets, and interview stage-tile counts (reusing `app.interview_rounds`,
the same grouping `app.routers.metrics` uses).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
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
    pace,
    percent,
    percentile,
    pick_bucket,
    prorated_goal,
    rank_rows,
    streak,
    week_window,
)
from app.deps import get_current_user
from app.interview_rounds import (
    BREAKDOWN_ORDER,
    count_interview_rounds,
    empty_breakdown,
    load_interviews_for_window,
)
from app.models import DailyPlan, Transaction, UserExtra
from app.plan_auto import interviews_by_day, interviews_split, weekly_plans_for
from app.routers._util import to_object_id
from app.routers.metrics import _display_name
from app.routers.transactions import _TOTALS_GROUP

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


def _target(total: int) -> Optional[int]:
    return total or None


@router.get("/week")
async def dashboard_week(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
    today: Optional[str] = Query(None),
) -> dict:
    sid = await _subject_id(user, userId)
    day = _today(today)
    tz = app_tz()
    monday, saturday, sunday = week_window(day)

    # Actuals: logged daily plans over the counting window (Mon-Sun);
    # interviews come from interview records.
    plans = await _logged_plans([sid], monday, sunday)
    self_sum = sum(p.done.bidsSelf for p in plans)
    bidder_sum = sum(p.done.bidsBidder for p in plans)
    bids_total = self_sum + bidder_sum

    # This week's weekly plan Goal is the target (0 means none set).
    weekly_plan = (await weekly_plans_for([sid], monday)).get(sid)
    goal = weekly_plan.goal if weekly_plan else None
    bids_target = _target(goal.bidsSelf + goal.bidsBidder) if goal else None
    interviews_target = _target(goal.interviewsSelf + goal.interviewsCaller) if goal else None

    # Stage tiles: interview rounds created by the user, dated in the window.
    iv_start = local_day_start_utc(monday, tz)
    iv_end = local_day_start_utc(sunday + timedelta(days=1), tz) - timedelta(microseconds=1)
    breakdown = empty_breakdown()
    canceled_total = 0
    for iv in await load_interviews_for_window([sid], iv_start, iv_end):
        _rounds, canceled, bd = count_interview_rounds(iv, iv_start, iv_end)
        canceled_total += canceled
        for k, v in bd.items():
            breakdown[k] = breakdown.get(k, 0) + v
    stages = [{"key": key, "label": label, "count": breakdown.get(key, 0)} for key, label in BREAKDOWN_ORDER]
    stages.append({"key": "canceled", "label": "Canceled", "count": canceled_total})

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
            "pace": pace(bids_total, bids_target, day),
            "percentile": percentile(bids_total, list(bids_scores.values())),
        },
        "interviews": {
            "done": interviews_done,
            "self": interviews[sid]["self"],
            "caller": interviews[sid]["caller"],
            "target": interviews_target,
            "pace": pace(interviews_done, interviews_target, day),
            "percentile": percentile(interviews_done, list(iv_scores.values())),
        },
        "stages": stages,
        "streak": streak_value,
        "hasWeeklyPlan": weekly_plan is not None,
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
    sid = await _subject_id(user, userId)
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
                "userId": sid,
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
        months.append({"period": period, "income": income, "outcome": outcome, "net": net})
        total += net

    return {"months": months, "total": round(total, 2)}


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

    def ranked(key: str) -> dict:
        board = rank_rows([(uid, _display_name(people[uid]), row[key]) for uid, row in stats.items()])
        mine = next((r for r in board if r["id"] == sid), None)
        return {
            "rows": [
                {"userId": str(r["id"]), "name": r["name"], "image": people[r["id"]].image or None,
                 "value": r["value"], "rank": r["rank"]}
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
            "interviews": ranked("interviews"),
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
