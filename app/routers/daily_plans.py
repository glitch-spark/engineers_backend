"""Daily plans: one Goal vs Done entry per person per calendar day."""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import ChecklistItems, Counts, DoneIn, DoneOverrides, next_working_day
from app.plan_auto import effective_daily_done, empty_day, interview_counts, profiles_by_region
from app.routers._plan_access import parse_body, parse_monday, parse_ymd, read_subject, require_writer
from app.slack_bot import delete_daily_plan_message, sync_daily_plan_message

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])


class TomorrowIn(BaseModel):
    goal: Counts = Field(default_factory=Counts)
    goalItems: ChecklistItems = []


class DayIn(BaseModel):
    goal: Counts = Field(default_factory=Counts)
    goalItems: ChecklistItems = []
    # Present when the person logs what they did; absent for a goal-only edit.
    done: Optional[DoneIn] = None
    doneOverrides: DoneOverrides = Field(default_factory=DoneOverrides)
    notes: str = Field(default="", max_length=2000)
    tomorrow: Optional[TomorrowIn] = None


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def _auto_for(uid: PydanticObjectId, start: date, end: date) -> tuple[dict, dict]:
    interviews = (await interview_counts([uid], start, end)).get(uid, {})
    profiles = (await profiles_by_region([uid])).get(uid, {})
    return interviews, profiles


def _day_out(uid, day: date, plan: Optional[DailyPlan], auto_day: dict, auto_profiles: dict) -> dict:
    done = effective_daily_done(plan, auto_day, auto_profiles)
    return {
        "date": day.isoformat(),
        "exists": plan is not None,
        "userId": str(uid),
        "goal": (plan.goal if plan else Counts()).model_dump(),
        "goalItems": [i.model_dump() for i in (plan.goalItems if plan else [])],
        "done": done.model_dump(),
        "auto": {
            "interviewsSelf": auto_day["self"],
            "interviewsCaller": auto_day["caller"],
            "stages": auto_day["stages"],
            "profiles": auto_profiles,
        },
        "doneOverrides": (plan.doneOverrides if plan else DoneOverrides()).model_dump(),
        "notes": plan.notes if plan else "",
        "loggedAt": plan.loggedAt.isoformat() if plan and plan.loggedAt else None,
    }


async def _find(uid, day: date) -> Optional[DailyPlan]:
    return await DailyPlan.find_one({"userId": uid, "date": _midnight(day)})


def _new_plan(uid, day: date) -> DailyPlan:
    when = _midnight(day)
    year, week_number, _key = app_week_key(when)
    return DailyPlan(userId=uid, date=when, year=year, weekNumber=week_number)


async def _save(plan: DailyPlan) -> None:
    if plan.id is None:
        await plan.insert()
    else:
        await plan.save()


async def _sync_slack(plan: DailyPlan, owner: UserExtra, tomorrow: Optional[DailyPlan], done: Counts) -> None:
    """Post or edit the logged day's Slack message; remember where it lives."""
    posted = await sync_daily_plan_message(plan, owner, tomorrow=tomorrow, done=done)
    if posted and posted != (plan.slackChannel, plan.slackTs):
        plan.slackChannel, plan.slackTs = posted
        await plan.save()


@router.get("")
async def list_week(
    user: Annotated[UserExtra, Depends(get_current_user)],
    weekStart: str = Query(...),
    userId: Optional[str] = Query(None),
) -> dict:
    """The week's days, Monday to Saturday, plus Sunday when it has an entry."""
    monday = parse_monday(weekStart)
    uid = await read_subject(user, userId)
    sunday = monday + timedelta(days=6)
    plans = await DailyPlan.find({
        "userId": uid,
        "date": {"$gte": _midnight(monday), "$lte": _midnight(sunday)},
    }).to_list()
    by_date = {p.date.date(): p for p in plans}
    interviews, profiles = await _auto_for(uid, monday, sunday)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    return {
        "weekStart": monday.isoformat(),
        "days": [_day_out(uid, d, by_date.get(d), interviews.get(d, empty_day()), profiles) for d in days],
    }


@router.get("/{day}")
async def get_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    when = parse_ymd(day, "date")
    uid = await read_subject(user, userId)
    interviews, profiles = await _auto_for(uid, when, when)
    return _day_out(uid, when, await _find(uid, when), interviews.get(when, empty_day()), profiles)


@router.put("/{day}")
async def put_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    """Save the day's goal, Done and notes; optionally set tomorrow's goal too."""
    require_writer(user)
    when = parse_ymd(day, "date")
    body = parse_body(DayIn, payload)
    uid = user.id
    interviews, profiles = await _auto_for(uid, when, when)

    plan = await _find(uid, when) or _new_plan(uid, when)
    plan.goal = body.goal
    plan.goalItems = body.goalItems
    plan.doneOverrides = body.doneOverrides
    plan.notes = body.notes
    if body.done is not None:
        plan.done = Counts(
            bidsSelf=body.done.bidsSelf,
            bidsBidder=body.done.bidsBidder,
            linkedin=body.done.linkedin,
            # Profiles managed are a snapshot of the Profiles page at log time.
            profiles=profiles,
        )
        if plan.loggedAt is None:
            plan.loggedAt = datetime.now(timezone.utc).replace(tzinfo=None)
    await _save(plan)

    next_day = next_working_day(when)
    tomorrow = await _find(uid, next_day)
    if body.tomorrow is not None:
        tomorrow = tomorrow or _new_plan(uid, next_day)
        tomorrow.goal = body.tomorrow.goal
        tomorrow.goalItems = body.tomorrow.goalItems
        await _save(tomorrow)

    auto_day = interviews.get(when, empty_day())
    if plan.loggedAt is not None:
        await _sync_slack(plan, user, tomorrow, effective_daily_done(plan, auto_day, profiles))

    tomorrow_out = None
    if body.tomorrow is not None:
        t_interviews, _ = await _auto_for(uid, next_day, next_day)
        tomorrow_out = _day_out(uid, next_day, tomorrow, t_interviews.get(next_day, empty_day()), profiles)
    return {"day": _day_out(uid, when, plan, auto_day, profiles), "tomorrow": tomorrow_out}


@router.delete("/{day}")
async def delete_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    require_writer(user)
    when = parse_ymd(day, "date")
    plan = await _find(user.id, when)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily plan not found")
    await delete_daily_plan_message(plan)
    await plan.delete()
    return {"message": "Daily plan deleted"}
