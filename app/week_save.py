"""Saving one person's weekly plan: shared by the person's own save and the admin's bulk save."""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Optional

from fastapi import HTTPException
from pydantic import AfterValidator, BaseModel, Field

from app.alerts import app_week_key
from app.models.plan_common import Counts, CountsIn, PlanItem, check_items
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import draft_week, week_snapshot

logger = logging.getLogger(__name__)


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


async def save_week(
    uid,
    monday: date,
    *,
    goal: Optional[Counts],
    routines: Optional[list[PlanItemIn]],
    weekly_goals: Optional[list[PlanItemIn]],
    recap: Optional[str],
) -> WeeklyPlan:
    """Insert or update the week. None = leave as stored (or as the carry-over draft on a first save)."""
    t0 = time.monotonic()
    plan = (await week_snapshot([uid], monday))[uid]["plan"]
    base = plan or await draft_week(uid, monday)  # a first save keeps the draft's routine ids
    if plan is None:
        start = _midnight(monday)
        year, week_number, _key = app_week_key(start)
        plan = WeeklyPlan(
            userId=uid,
            weekStart=start,
            year=year,
            weekNumber=week_number,
            startDate=start,
            endDate=start + timedelta(days=7) - timedelta(milliseconds=1),
        )
        if base is not None:
            plan.goal, plan.routines, plan.weeklyGoals = base.goal, base.routines, base.weeklyGoals
    if goal is not None:
        plan.goal = goal
    if routines is not None:
        plan.routines = _merge_items(routines, base.routines if base else [], "routines")
    if weekly_goals is not None:
        plan.weeklyGoals = _merge_items(weekly_goals, base.weeklyGoals if base else [], "weeklyGoals")
    if recap is not None:
        plan.recapNotes = recap
    if plan.id is None:
        await plan.insert()
    else:
        # Replaces the whole document, so a legacy plan is rewritten in the new shape.
        await plan.save()
    logger.info("week save: user=%s week=%s routines=%d goals=%d ms=%d", uid, monday, len(plan.routines),
                len(plan.weeklyGoals), (time.monotonic() - t0) * 1000)
    return plan
