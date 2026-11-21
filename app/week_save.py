"""Saving one person's weekly plan: shared by the person's own save and the admin's bulk save."""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional

from fastapi import HTTPException
from pydantic import AfterValidator, BaseModel, Field

from app.alerts import app_week_key
from app.models.plan_common import MAX_ITEMS, Counts, CountsIn, PlanItem, check_items
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import draft_week, week_snapshot

logger = logging.getLogger(__name__)

Added = Literal["admin", "self"]
PLANNED_BY_ADMIN = "Your week is planned by your admin"


class PlanItemIn(PlanItem):
    """A routine / weekly goal as sent: no id = new item (the server assigns one)."""

    id: Optional[str] = None


PlanItemsIn = Annotated[list[PlanItemIn], AfterValidator(check_items)]


class WeekIn(BaseModel):
    goal: CountsIn = Field(default_factory=CountsIn)
    routines: PlanItemsIn = []
    weeklyGoals: PlanItemsIn = []
    recapNotes: str = Field(default="", max_length=4000)


def _merge_items(sent: list[PlanItemIn], old: list[PlanItem], what: str, added_by: Added,
                 locked: bool = False) -> list[PlanItem]:
    """The sent items in order (ids kept), then the old ones left out, archived: items are never deleted.

    A kept id with a new kind is remove + add: past days keep the old item with its old kind.
    `addedBy` is never taken from the body: a known id keeps its stored value (a kind change too), a new item gets
    `added_by`. `locked` (an admin-planned person's save): stored admin items stay exactly as stored whatever the
    body says. Result = sent self / new items and stored admin items in body order, then stored admin items missing
    from the body (live ones stay live), then the archived ones. Over MAX_ITEMS live items -> 422."""
    by_id = {i.id: i for i in old}
    seen: set[str] = set()
    out: list[PlanItem] = []
    for i in sent:
        if i.id is not None and (i.id not in by_id or i.id in seen):
            raise HTTPException(status_code=422, detail=f"{what}: unknown or repeated item id {i.id}")
        seen.add(i.id)
        prev = by_id.get(i.id)
        if locked and prev is not None and prev.addedBy == "admin":
            out.append(prev)
            continue
        fields = i.model_dump(exclude={"id", "addedBy"})
        who = prev.addedBy if prev is not None else added_by
        keep_id = prev is not None and prev.kind == i.kind
        out.append(PlanItem(**fields, addedBy=who, **({"id": i.id} if keep_id else {})))
    kept = {i.id for i in out}
    rest = [o for o in old if o.id not in kept]
    if locked:
        out += [o for o in rest if o.addedBy == "admin" and not o.archived]
        rest = [o for o in rest if o.addedBy != "admin" or o.archived]
    out += [o.model_copy(update={"archived": True}) for o in rest]
    if sum(not i.archived for i in out) > MAX_ITEMS:
        raise HTTPException(status_code=422, detail=f"{what}: at most {MAX_ITEMS} checklist items")
    return out


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
    added_by: Added,
    locked: bool = False,
) -> WeeklyPlan:
    """Insert or update the week. None = leave as stored (or as the carry-over draft on a first save).

    `added_by` marks new items; `locked` = the admin-planned person's save (see `_merge_items`): the goal and the
    admin's items stay as stored."""
    t0 = time.monotonic()
    plan = (await week_snapshot([uid], monday))[uid]["plan"]
    if locked:
        goal = None
        if plan is None:  # the admin deleted the week after the caller's check: no person-made plan
            raise HTTPException(status_code=403, detail=PLANNED_BY_ADMIN)
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
        plan.routines = _merge_items(routines, base.routines if base else [], "routines", added_by, locked)
    if weekly_goals is not None:
        plan.weeklyGoals = _merge_items(weekly_goals, base.weeklyGoals if base else [], "weeklyGoals", added_by, locked)
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
