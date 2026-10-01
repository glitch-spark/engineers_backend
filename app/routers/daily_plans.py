"""Daily plans: the day's goal set in the morning, followed up with what was done."""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import BidsIn, ChecklistItems, Counts
from app.plan_auto import effective_daily_done, empty_day, interview_counts
from app.routers._plan_access import parse_body, parse_monday, parse_ymd, read_subject, require_writer
from app.slack_bot import delete_daily_plan_message, sync_daily_plan_message

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])


class DayIn(BaseModel):
    goal: BidsIn = Field(default_factory=BidsIn)
    goalItems: ChecklistItems = []
    # Present on a follow-up (what was actually done); absent when only the goal is saved.
    done: Optional[BidsIn] = None
    notes: str = Field(default="", max_length=2000)


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _day_out(uid, day: date, plan: Optional[DailyPlan], auto_day: dict) -> dict:
    return {
        "date": day.isoformat(),
        "exists": plan is not None,
        "userId": str(uid),
        "goal": (plan.goal if plan else Counts()).model_dump(),
        "goalItems": [i.model_dump() for i in (plan.goalItems if plan else [])],
        "done": effective_daily_done(plan, auto_day).model_dump(),
        "interviews": {"self": auto_day["self"], "caller": auto_day["caller"], "stages": auto_day["stages"]},
        "notes": plan.notes if plan else "",
        "loggedAt": plan.loggedAt.isoformat() if plan and plan.loggedAt else None,
    }


async def _interviews(uid, start: date, end: date) -> dict:
    return (await interview_counts([uid], start, end)).get(uid, {})


async def _find(uid, day: date) -> Optional[DailyPlan]:
    return await DailyPlan.find_one({"userId": uid, "date": _midnight(day)})


async def _sync_slack(plan: DailyPlan, owner: UserExtra, done: Counts) -> None:
    """Post or edit the day's follow-up message; remember where it lives."""
    posted = await sync_daily_plan_message(plan, owner, done=done)
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
    interviews = await _interviews(uid, monday, sunday)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    return {
        "weekStart": monday.isoformat(),
        "days": [_day_out(uid, d, by_date.get(d), interviews.get(d, empty_day())) for d in days],
    }


@router.get("/{day}")
async def get_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    when = parse_ymd(day, "date")
    uid = await read_subject(user, userId)
    interviews = await _interviews(uid, when, when)
    return _day_out(uid, when, await _find(uid, when), interviews.get(when, empty_day()))


@router.put("/{day}")
async def put_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    """Save the day's goal; with `done`, also save the follow-up and post it to Slack."""
    require_writer(user)
    when = parse_ymd(day, "date")
    body = parse_body(DayIn, payload)
    plan = await _find(user.id, when)
    if plan is None:
        year, week_number, _key = app_week_key(_midnight(when))
        plan = DailyPlan(userId=user.id, date=_midnight(when), year=year, weekNumber=week_number)
    plan.goal = Counts(bidsSelf=body.goal.bidsSelf, bidsBidder=body.goal.bidsBidder)
    plan.goalItems = body.goalItems
    plan.notes = body.notes
    if body.done is not None:
        plan.done = Counts(bidsSelf=body.done.bidsSelf, bidsBidder=body.done.bidsBidder)
        if plan.loggedAt is None:
            plan.loggedAt = datetime.now(timezone.utc).replace(tzinfo=None)
    if plan.id is None:
        await plan.insert()
    else:
        await plan.save()

    auto_day = (await _interviews(user.id, when, when)).get(when, empty_day())
    if body.done is not None:
        await _sync_slack(plan, user, effective_daily_done(plan, auto_day))
    return _day_out(user.id, when, plan, auto_day)


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
