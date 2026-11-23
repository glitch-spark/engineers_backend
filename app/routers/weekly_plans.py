"""Weekly plans: one Goal vs Done entry per person per Monday-start week: targets, routines and weekly goals.

A week without a saved plan reads as a draft carried over from last week (`plan_auto.draft_week`); nothing is saved
until the person saves. Done is computed from the week's daily plans and interview records
(`app.plan_auto.week_snapshot`).
"""

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from beanie import PydanticObjectId
from pydantic import BaseModel, Field, ValidationError
from app.deps import get_current_user, require_admin
from app.models import UserExtra
from app.models.plan_common import Counts
from app.models.weekly_plan import WeeklyPlan
from app.plan_auto import (day_targets, draft_week, goal_progress, items_tally, week_snapshot, weeks_or_drafts,
                           weekly_plans_for)
from app.week_save import PLANNED_BY_ADMIN, PlanItemIn, PlanItemsIn, WeekIn, _merge_items, save_week  # noqa: F401  (re-exported)
from app.routers._plan_access import parse_body, parse_monday, read_subject, require_writer

log = logging.getLogger(__name__)

router = APIRouter(prefix="/weekly-plans", tags=["weekly-plans"])


class TeamGoalIn(BaseModel):
    bidsSelf: int = Field(ge=0)
    bidsBidder: int = Field(ge=0)
    interviewsSelf: int = Field(ge=0)


class TeamRowIn(BaseModel):
    userId: str
    plansOwnWeek: bool
    goal: TeamGoalIn
    routines: PlanItemsIn
    weeklyGoals: PlanItemsIn
    exists: bool = True  # False = the grid loaded a carry-over draft; an older client omits it
    removedIds: list[str] = []  # own (self) items the admin removed; any other missing self item stays live


class TeamSaveIn(BaseModel):
    plans: list[dict] = Field(min_length=1, max_length=100)


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


@router.get("/team/{week_start}")
async def get_team_week(week_start: str, admin: Annotated[UserExtra, Depends(require_admin)]) -> dict:
    monday = parse_monday(week_start)
    users = await UserExtra.find({"role": {"$ne": "admin"}, "isActive": {"$ne": False}}).to_list()
    users.sort(key=lambda u: (not u.name, (u.name or "").lower()))
    plans = await weeks_or_drafts([u.id for u in users], monday)
    out = []
    for u in users:
        plan = plans.get(u.id)
        goal = plan.goal if plan else Counts()
        out.append({
            "userId": str(u.id),
            "name": u.name,
            "plansOwnWeek": u.plansOwnWeek,
            "exists": plan is not None and plan.id is not None,  # a carry-over draft has no id
            "goal": {"bidsSelf": goal.bidsSelf, "bidsBidder": goal.bidsBidder, "interviewsSelf": goal.interviewsSelf},
            "routines": [i.model_dump() for i in plan.routines if not i.archived] if plan else [],
            "weeklyGoals": [i.model_dump() for i in plan.weeklyGoals if not i.archived] if plan else [],
        })
    return {"weekStart": monday.isoformat(), "users": out}


def _row_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        msg = exc.errors()[0].get("msg", "Invalid row")
        return msg.removeprefix("Value error, ")
    return str(exc.detail)


@router.put("/team/{week_start}")
async def put_team_week(
    week_start: str,
    admin: Annotated[UserExtra, Depends(require_admin)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    t0 = time.monotonic()
    monday = parse_monday(week_start)
    body = parse_body(TeamSaveIn, payload)
    ids = list({PydanticObjectId(u) for raw in body.plans if PydanticObjectId.is_valid(u := str(raw.get("userId", "")))})
    users = {u.id: u for u in await UserExtra.find({"_id": {"$in": ids}}).to_list()} if ids else {}
    known = await weeks_or_drafts(list(users), monday) if users else {}
    seen: set[str] = set()

    async def one(raw: dict) -> dict:
        uid_str = str(raw.get("userId", ""))
        try:
            if uid_str in seen:
                raise HTTPException(status_code=422, detail="Repeated row for this person")
            seen.add(uid_str)
            await _save_team_row(raw, monday, users, known)
            return {"userId": uid_str, "ok": True, "error": None}
        except (HTTPException, ValidationError) as exc:
            return {"userId": uid_str, "ok": False, "error": _row_error(exc)}
        except Exception:
            log.exception("team week save: row failed week=%s user=%s", monday, uid_str)
            return {"userId": uid_str, "ok": False, "error": "Could not save"}

    # Rows are distinct people (repeats refused above, before any await), so they save concurrently.
    results = await asyncio.gather(*(one(raw) for raw in body.plans))
    ok = sum(r["ok"] for r in results)
    log.info("team week save: admin=%s week=%s ok=%d failed=%d ms=%d", admin.id, monday, ok, len(results) - ok,
             (time.monotonic() - t0) * 1000)
    return {"results": list(results)}


async def _save_team_row(raw: dict, monday: date, users: dict, known: dict) -> None:
    """`users` / `known`: the rows' people and their `week_or_draft`, loaded once for the whole save."""
    row = TeamRowIn.model_validate(raw)
    user = users.get(PydanticObjectId(row.userId)) if PydanticObjectId.is_valid(row.userId) else None
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=422, detail="Admins have no plan")
    stored = known.get(user.id)  # the saved plan, else the carry-over draft
    if not row.exists and stored is not None and stored.id is not None:  # the person (or another admin) saved meanwhile
        raise HTTPException(status_code=409, detail="Saved since you loaded the grid — reload to see it")
    base = stored.goal if stored else Counts()
    empty = not (row.goal.bidsSelf or row.goal.bidsBidder or row.goal.interviewsSelf or row.routines or row.weeklyGoals)
    if (stored is not None and stored.id is not None) or not empty:  # nothing to plan yet: don't create an empty week
        if row.goal.bidsSelf == base.bidsSelf:  # total unchanged: keep the stored region split
            goal = base.model_copy(update={"bidsBidder": row.goal.bidsBidder, "interviewsSelf": row.goal.interviewsSelf})
        else:
            goal = Counts(bidsSelf=row.goal.bidsSelf, bidsBidder=row.goal.bidsBidder,
                          interviewsSelf=row.goal.interviewsSelf, interviewsCaller=base.interviewsCaller)
        await save_week(user.id, monday, goal=goal, routines=row.routines, weekly_goals=row.weeklyGoals, recap=None,
                        added_by="admin", removed_ids=set(row.removedIds), known=stored)
    if row.plansOwnWeek != user.plansOwnWeek:
        await UserExtra.find_one(UserExtra.id == user.id).update({"$set": {"plansOwnWeek": row.plansOwnWeek}})


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
                        weekly_goals=body.weeklyGoals, recap=body.recapNotes, added_by="self")
    else:  # the admin plans the week: own items and the recap, only on a plan the admin saved
        if (await _snapshot(user.id, monday))["plan"] is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLANNED_BY_ADMIN)
        await save_week(user.id, monday, goal=None, routines=body.routines, weekly_goals=body.weeklyGoals,
                        recap=body.recapNotes, added_by="self", locked=True)
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
