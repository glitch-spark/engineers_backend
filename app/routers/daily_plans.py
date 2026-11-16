"""Daily plans: each day is followed up with what was done.

The day's goal comes from the weekly plan's daily goal (set once a week); a
day keeps its own copy of that goal once it is followed up.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.alerts import app_week_key
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import BidsIn, ChecklistItems, Counts, DoneCounts, bids_of
from app.dashboard_stats import app_tz
from app.models.plan_common import monday_of
from app.plan_auto import day_goal, effective_daily_done, empty_day, interview_counts, weekly_plans_for
from app.plan_profiles import derive_self_bids, owner_profiles, profile_regions
from app import self_bids
from app.routers._plan_access import parse_body, parse_monday, parse_ymd, read_subject, require_writer
from app.slack_bot import delete_daily_plan_message, sync_daily_plan_message

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])


class DayIn(BaseModel):
    """A follow-up: bids done, the daily goal lines with ticks, and notes."""

    goalItems: ChecklistItems = []
    done: BidsIn = Field(default_factory=BidsIn)
    notes: str = Field(default="", max_length=2000)


def _today() -> date:
    return datetime.now(app_tz()).date()


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _day_out(uid, day: date, plan: Optional[DailyPlan], auto_day: dict, weekly, profiles: Optional[list] = None) -> dict:
    """`profiles` is the owner's; the week view sends them once, so it passes None and the key is left out."""
    goal, items = day_goal(plan, weekly)
    out = {
        "date": day.isoformat(),
        "exists": plan is not None,
        "userId": str(uid),
        "goal": goal.model_dump(),
        "goalItems": [i.model_dump() for i in items],
        "done": effective_daily_done(plan, auto_day).model_dump(),
        "interviews": {"self": auto_day["self"], "caller": auto_day["caller"], "stages": auto_day["stages"]},
        "notes": plan.notes if plan else "",
        "loggedAt": plan.loggedAt.isoformat() if plan and plan.loggedAt else None,
    }
    if profiles is not None:
        out["profiles"] = profiles
    return out


async def _interviews(uid, start: date, end: date) -> dict:
    return (await interview_counts([uid], start, end)).get(uid, {})


async def _weekly(uid, day: date):
    return (await weekly_plans_for([uid], monday_of(day))).get(uid)


async def _find(uid, day: date) -> Optional[DailyPlan]:
    return await DailyPlan.find_one({"userId": uid, "date": _midnight(day)})


async def _sync_slack(plan: DailyPlan, owner: UserExtra, done: Counts, rounds: dict) -> None:
    """Post or edit the day's follow-up message; remember where it lives."""
    posted = await sync_daily_plan_message(plan, owner, done=done, rounds=rounds)
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
    weekly = await _weekly(uid, monday)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    return {
        "weekStart": monday.isoformat(),
        "profiles": await owner_profiles(uid),
        "days": [_day_out(uid, d, by_date.get(d), interviews.get(d, empty_day()), weekly) for d in days],
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
    out = _day_out(
        uid, when, await _find(uid, when), interviews.get(when, empty_day()), await _weekly(uid, when),
        await owner_profiles(uid),
    )
    return await _with_tracked(out, uid, when)


async def _with_tracked(out: dict, uid, when: date) -> dict:
    """The extension's self bids for the day (shown next to typed in the pilot) and the profiles already on tracked
    counts (their typed number is "other applies")."""
    out["trackedSelfByProfile"] = await self_bids.tracked_for_day(uid, when)
    out["trackedProfiles"] = sorted(await self_bids.switched_profiles(uid, when))
    plan_done = out.get("done") or {}
    out["selfOtherByProfile"] = (plan_done.get("bidsSelfOtherByProfile") if isinstance(plan_done, dict) else None) or {}
    return out


@router.put("/{day}")
async def put_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    payload: Annotated[dict[str, Any], Body()] = None,
) -> dict:
    """Save the day's follow-up (what was done) and post it to Slack."""
    require_writer(user)
    when = parse_ymd(day, "date")
    body = parse_body(DayIn, payload)
    if when > _today():
        raise HTTPException(status_code=422, detail="A day can be followed up from that day on")
    plan = await _find(user.id, when)
    weekly = await _weekly(user.id, when)
    if plan is None:
        year, week_number, _key = app_week_key(_midnight(when))
        plan = DailyPlan(userId=user.id, date=_midnight(when), year=year, weekNumber=week_number)
    # Keep the goal this day was followed up against, even if the week's daily goal changes later.
    goal, _items = day_goal(plan, weekly)
    plan.goal = Counts(**bids_of(goal))
    plan.goalItems = body.goalItems
    profiles = await owner_profiles(user.id)  # one read: validation and the response
    typed = {pid: n for pid, n in body.done.bidsSelfByProfile.items() if n}
    # Profiles on tracked self bids: the typed number is "other applies", added to the tracked count.
    switched = await self_bids.switched_profiles(user.id, when)
    other = {pid: n for pid, n in typed.items() if pid in switched}
    by = await self_bids.day_self_bids(user.id, when, typed, other) if switched else typed
    if by:
        regions = await profile_regions(profiles, by)
        by = {pid: by[pid] for pid in regions}
        plan.done = DoneCounts(**{**bids_of(body.done), **derive_self_bids(by, regions)}, bidsSelfByProfile=by,
                               bidsSelfOtherByProfile=other)
    else:
        plan.done = DoneCounts(**bids_of(body.done), bidsSelfOtherByProfile=other)
    plan.notes = body.notes
    if plan.loggedAt is None:
        plan.loggedAt = datetime.now(timezone.utc).replace(tzinfo=None)
    if plan.id is None:
        await plan.insert()
    else:
        await plan.save()

    auto_day = (await _interviews(user.id, when, when)).get(when, empty_day())
    await _sync_slack(plan, user, effective_daily_done(plan, auto_day), auto_day["rounds"])
    return await _with_tracked(_day_out(user.id, when, plan, auto_day, weekly, profiles), user.id, when)


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
