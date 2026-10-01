"""Weekly plans: one Goal vs Done entry per person per Monday-start week.

Done is computed from the week's daily plans and interview records
(`app.plan_auto.week_snapshot`).
"""

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.plan_common import BidsIn, ChecklistItem, ChecklistItems, Counts
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import day_goal, week_snapshot
from app.routers._plan_access import parse_body, parse_monday, read_subject, require_writer

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


class WeekIn(BaseModel):
    goal: Counts = Field(default_factory=Counts)
    goalItems: ChecklistItems = []
    dailyGoal: BidsIn = Field(default_factory=BidsIn)
    dailyGoalItems: ChecklistItems = []
    recapNotes: str = Field(default="", max_length=4000)


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _sum_stages(day_auto: dict) -> dict:
    out: dict = {"self": {}, "caller": {}}
    for entry in day_auto.values():
        for who in ("self", "caller"):
            for group, n in entry["stages"][who].items():
                out[who][group] = out[who].get(group, 0) + n
    return out


def _week_out(uid, monday: date, snap: dict) -> dict:
    plan: Optional[WeeklyPlan] = snap["plan"]
    by_date = {p.date.date(): p for p in snap["days"]}
    sunday = monday + timedelta(days=6)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    goals = {d: day_goal(by_date.get(d), plan) for d in days}
    return {
        "weekStart": monday.isoformat(),
        "exists": plan is not None,
        "userId": str(uid),
        "goal": (plan.goal if plan else Counts()).model_dump(),
        "goalItems": [i.model_dump() for i in (plan.goalItems if plan else [])],
        "dailyGoal": (plan.dailyGoal if plan else Counts()).model_dump(),
        "dailyGoalItems": [i.model_dump() for i in (plan.dailyGoalItems if plan else [])],
        "recapNotes": plan.recapNotes if plan else "",
        "done": snap["done"].model_dump(),
        "stages": _sum_stages(snap["dayAuto"]),
        "days": [
            {
                "date": d.isoformat(),
                "logged": d in by_date and by_date[d].loggedAt is not None,
                "goal": goals[d][0].model_dump(),
                "done": snap["dayDone"].get(d, Counts()).model_dump(),
                "itemsDone": sum(1 for i in goals[d][1] if i.done),
                "itemsTotal": len(goals[d][1]),
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
    return _week_out(uid, monday, await _snapshot(uid, monday))


@router.get("/{week_start}/previous-goals")
async def previous_goals(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Last week's goal and unticked items, to start this week from."""
    monday = parse_monday(week_start)
    plan = (await _snapshot(user.id, monday - timedelta(days=7)))["plan"]
    if plan is None:
        return {"goal": None, "goalItems": [], "dailyGoal": None, "dailyGoalItems": []}
    return {
        "goal": plan.goal.model_dump(),
        "goalItems": [{"text": i.text, "done": False} for i in plan.goalItems if not i.done],
        "dailyGoal": plan.dailyGoal.model_dump(),
        "dailyGoalItems": [{"text": i.text, "done": False} for i in plan.dailyGoalItems],
    }


@router.put("/{week_start}")
async def put_week(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    require_writer(user)
    monday = parse_monday(week_start)
    body = parse_body(WeekIn, payload)
    plan = (await _snapshot(user.id, monday))["plan"]
    if plan is None:
        start = _midnight(monday)
        year, week_number, _key = app_week_key(start)
        plan = WeeklyPlan(
            userId=user.id,
            weekStart=start,
            year=year,
            weekNumber=week_number,
            startDate=start,
            endDate=start + timedelta(days=7) - timedelta(milliseconds=1),
        )
    plan.goal = body.goal
    plan.goalItems = body.goalItems
    plan.dailyGoal = Counts(bidsSelf=body.dailyGoal.bidsSelf, bidsBidder=body.dailyGoal.bidsBidder)
    # Daily lines are a template: ticks happen on each day.
    plan.dailyGoalItems = [ChecklistItem(text=i.text) for i in body.dailyGoalItems]
    plan.recapNotes = body.recapNotes
    if plan.id is None:
        await plan.insert()
    else:
        # Replaces the whole document, so a legacy plan is rewritten in the new shape.
        await plan.save()
    return _week_out(user.id, monday, await _snapshot(user.id, monday))


@router.delete("/{week_start}")
async def delete_week(
    week_start: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    require_writer(user)
    monday = parse_monday(week_start)
    plan = (await _snapshot(user.id, monday))["plan"]
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Weekly plan not found")
    await plan.delete()
    return {"message": "Weekly plan deleted"}
