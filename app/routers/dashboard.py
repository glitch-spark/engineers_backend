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
    local_day_start_utc,
    month_keys,
    pace,
    percentile,
    pick_bucket,
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
from app.plan_auto import effective_interviews_by_day, weekly_plans_for
from app.routers._util import to_object_id
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


async def _logged_plans(user_ids: list, start: date, end: date) -> list[DailyPlan]:
    """Daily plans whose Done was logged (goal-only entries don't count)."""
    return await DailyPlan.find(
        {
            "userId": {"$in": user_ids},
            "loggedAt": {"$ne": None},
            "date": {"$gte": _midnight(start), "$lte": _midnight(end)},
        }
    ).to_list()


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
    # interviews are each day's Done (records, or the day's override).
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
        {"loggedAt": {"$ne": None}, "date": {"$gte": _midnight(population_start), "$lte": _midnight(day)}},
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
    interviews = await effective_interviews_by_day(members, monday, sunday)
    iv_scores = {uid: sum(interviews.get(uid, {}).values()) for uid in population}
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
    for day, count in (await effective_interviews_by_day([sid], start, end)).get(sid, {}).items():
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
