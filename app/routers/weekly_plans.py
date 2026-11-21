"""Weekly plans: one Goal vs Done entry per person per Monday-start week: targets, routines and weekly goals.

A week without a saved plan reads as a draft carried over from last week (`plan_auto.draft_week`); nothing is saved
until the person saves. Done is computed from the week's daily plans and interview records
(`app.plan_auto.week_snapshot`).
"""

from datetime import date, timedelta
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from app.deps import get_current_user
from app.models import UserExtra
from app.models.plan_common import Counts
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import day_targets, draft_week, goal_progress, items_tally, week_snapshot
from app.week_save import PlanItemIn, PlanItemsIn, WeekIn, _merge_items, save_week  # noqa: F401  (re-exported)
from app.routers._plan_access import parse_body, parse_monday, read_subject, require_writer

PLANNED_BY_ADMIN = "Your week is planned by your admin"

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


def _sum_stages(day_auto: dict) -> dict:
    out: dict = {"self": {}, "caller": {}}
    for entry in day_auto.values():
        for who in ("self", "caller"):
            for group, n in entry["stages"][who].items():
                out[who][group] = out[who].get(group, 0) + n
    return out


def _week_out(uid, monday: date, snap: dict, draft: Optional[WeeklyPlan] = None, *, plans_own_week: bool) -> dict:
    """`draft` (carry-over from last week) stands in for a week without a saved plan."""
    plan: Optional[WeeklyPlan] = snap["plan"] or draft
    by_date = {p.date.date(): p for p in snap["days"]}
    sunday = monday + timedelta(days=6)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    tally = {d: items_tally(plan, by_date.get(d), d) for d in days}
    return {
        "weekStart": monday.isoformat(),
        "exists": snap["plan"] is not None,
        "draft": snap["plan"] is None and draft is not None,
        "userId": str(uid),
        "plansOwnWeek": plans_own_week,
        "goal": (plan.goal if plan else Counts()).model_dump(),
        "routines": [i.model_dump() for i in (plan.routines if plan else [])],
        "weeklyGoals": [i.model_dump() for i in (plan.weeklyGoals if plan else [])],
        "progress": goal_progress(plan, snap["days"]) if plan else {},
        "recapNotes": plan.recapNotes if plan else "",
        "done": snap["done"].model_dump(),
        "stages": _sum_stages(snap["dayAuto"]),
        "days": [
            {
                "date": d.isoformat(),
                "logged": d in by_date and by_date[d].loggedAt is not None,
                "submitted": d in by_date and by_date[d].submittedAt is not None,
                "goal": day_targets(by_date.get(d), plan, d).model_dump(),
                "done": snap["dayDone"].get(d, Counts()).model_dump(),
                "itemsDone": tally[d][0],
                "itemsTotal": tally[d][1],
            }
            for d in days
        ],
    }


async def _snapshot(uid, monday: date) -> dict:
    return (await week_snapshot([uid], monday))[uid]


@router.get("/{week_start}")
async def get_week(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    monday = parse_monday(week_start)
    uid = await read_subject(user, userId)
    snap = await _snapshot(uid, monday)
    plans_own = user.plansOwnWeek if uid == user.id else (await UserExtra.get(uid)).plansOwnWeek
    return _week_out(uid, monday, snap, None if snap["plan"] else await draft_week(uid, monday), plans_own_week=plans_own)


@router.put("/{week_start}")
async def put_week(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    require_writer(user)
    monday = parse_monday(week_start)
    body = parse_body(WeekIn, payload)
    if user.plansOwnWeek:
        await save_week(user.id, monday, goal=Counts(**body.goal.model_dump()), routines=body.routines,
                        weekly_goals=body.weeklyGoals, recap=body.recapNotes)
    else:  # the admin plans the week: only the recap, and only on a plan the admin saved
        if (await _snapshot(user.id, monday))["plan"] is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLANNED_BY_ADMIN)
        await save_week(user.id, monday, goal=None, routines=None, weekly_goals=None, recap=body.recapNotes)
    return _week_out(user.id, monday, await _snapshot(user.id, monday), plans_own_week=user.plansOwnWeek)


@router.delete("/{week_start}")
async def delete_week(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    require_writer(user)
    if not user.plansOwnWeek:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLANNED_BY_ADMIN)
    monday = parse_monday(week_start)
    plan = (await _snapshot(user.id, monday))["plan"]
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")
    await plan.delete()
    return {"message": "Weekly plan deleted"}
