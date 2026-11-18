"""Weekly plans: one Goal vs Done entry per person per Monday-start week: targets, routines and weekly goals.

A week without a saved plan reads as a draft carried over from last week (`plan_auto.draft_week`); nothing is saved
until the person saves. Done is computed from the week's daily plans and interview records
(`app.plan_auto.week_snapshot`).
"""

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import AfterValidator, BaseModel, Field

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.plan_common import Counts, CountsIn, PlanItem, check_items
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import day_targets, draft_week, goal_progress, items_tally, week_snapshot
from app.routers._plan_access import parse_body, parse_monday, read_subject, require_writer

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


class PlanItemIn(PlanItem):
    """A routine / weekly goal as sent: no id = new item (the server assigns one)."""

    id: Optional[str] = None


PlanItemsIn = Annotated[list[PlanItemIn], AfterValidator(check_items)]


class WeekIn(BaseModel):
    goal: CountsIn = Field(default_factory=CountsIn)
    routines: PlanItemsIn = []
    weeklyGoals: PlanItemsIn = []
    recapNotes: str = Field(default="", max_length=4000)


def _merge_items(sent: list[PlanItemIn], old: list[PlanItem], what: str) -> list[PlanItem]:
    """The sent items in order (ids kept), then the old ones left out, archived: items are never deleted."""
    known = {i.id for i in old}
    out: list[PlanItem] = []
    for i in sent:
        if i.id is not None and (i.id not in known or any(o.id == i.id for o in out)):
            raise HTTPException(status_code=422, detail=f"{what}: unknown or repeated item id {i.id}")
        fields = i.model_dump(exclude={"id"})
        out.append(PlanItem(**fields, id=i.id) if i.id else PlanItem(**fields))
    kept = {i.id for i in out}
    return out + [o.model_copy(update={"archived": True}) for o in old if o.id not in kept]


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _sum_stages(day_auto: dict) -> dict:
    out: dict = {"self": {}, "caller": {}}
    for entry in day_auto.values():
        for who in ("self", "caller"):
            for group, n in entry["stages"][who].items():
                out[who][group] = out[who].get(group, 0) + n
    return out


def _week_out(uid, monday: date, snap: dict, draft: Optional[WeeklyPlan] = None) -> dict:
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
    return _week_out(uid, monday, snap, None if snap["plan"] else await draft_week(uid, monday))


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
    base = plan or await draft_week(user.id, monday)  # a first save keeps the draft's routine ids
    routines = _merge_items(body.routines, base.routines if base else [], "routines")
    weekly_goals = _merge_items(body.weeklyGoals, base.weeklyGoals if base else [], "weeklyGoals")
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
    plan.goal = Counts(**body.goal.model_dump())
    plan.routines = routines
    plan.weeklyGoals = weekly_goals
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
