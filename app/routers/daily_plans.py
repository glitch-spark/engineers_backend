"""Daily plans: each day is followed up with what was done.

The day's targets are derived from the weekly plan (or its carry-over draft) unless the day stored its own on submit;
its items are the week's routines and weekly goals with that day's results (`app.plan_auto`). Saving goes through
`app.plan_save.save_day`, shared with the Slack standup form.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app import plan_bidder, self_bids
from app.dashboard_stats import app_tz
from app.deps import get_current_user
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import Counts, monday_of
from app.plan_auto import (
    day_items, day_targets, effective_daily_done, empty_day, goal_progress, interview_counts, week_or_draft,
)
from app.plan_profiles import REGION_FIELD, owner_profiles
from app.plan_save import DayIn, ProgressIn, save_day, save_progress
from app.routers.interviews import day_interviews, follow_ups_summary
from app.routers._plan_access import parse_body, parse_monday, parse_ymd, read_subject, require_writer
from app.slack_bot import delete_daily_plan_message, sync_daily_plan_message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/daily-plans", tags=["daily-plans"])


def _today() -> date:
    return datetime.now(app_tz()).date()


def _now() -> datetime:
    """Naive UTC now."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _item_out(e: dict) -> dict:
    item, result = e["item"], e["result"]
    return {"id": item.id, "text": item.text, "kind": item.kind, "scope": e["scope"], "target": item.target,
            "options": item.options, "archived": item.archived, "addedBy": item.addedBy, "result": result.model_dump() if result else None}


def _day_out(uid, day: date, plan: Optional[DailyPlan], auto_day: dict, weekly, week_days: list,
             bidder_auto: int, profiles: Optional[list] = None) -> dict:
    """`week_days` are the week's saved days (for weekly goal progress); `profiles` is the owner's — the week view
    sends them once, so it passes None and the key is left out."""
    out = {
        "date": day.isoformat(),
        "exists": plan is not None,
        "userId": str(uid),
        "targets": day_targets(plan, weekly, day).model_dump(),
        "items": [_item_out(e) for e in day_items(weekly, plan, day)],
        "progress": goal_progress(weekly, week_days) if weekly else {},
        "focus": plan.focus if plan else [],
        "done": effective_daily_done(plan, auto_day).model_dump(),
        "bidsBidderAuto": bidder_auto,
        "interviews": {"self": auto_day["self"], "caller": auto_day["caller"], "stages": auto_day["stages"]},
        "notes": plan.notes if plan else "",
        "loggedAt": plan.loggedAt.isoformat() if plan and plan.loggedAt else None,
        "submitted": bool(plan and plan.submittedAt),
        "weekSaved": weekly is not None and weekly.id is not None,  # the carry-over draft has no id
    }
    if profiles is not None:
        out["profiles"] = profiles
    return out


async def _week_days(uid, monday: date) -> list[DailyPlan]:
    return await DailyPlan.find({
        "userId": uid,
        "date": {"$gte": _midnight(monday), "$lte": _midnight(monday + timedelta(days=6))},
    }).to_list()


async def _interviews(uid, start: date, end: date) -> dict:
    return (await interview_counts([uid], start, end)).get(uid, {})


async def _find(uid, day: date) -> Optional[DailyPlan]:
    return await DailyPlan.find_one({"userId": uid, "date": _midnight(day)})


# A first post in flight holds `slackTs = "posting:<epoch s>"` so concurrent saves (app, Slack form, write-throughs)
# don't post twice. Real Slack ts values start with a digit, so they never match the claim prefix.
POSTING = "posting:"
CLAIM_STALE_S = 120  # a claim this old belongs to a save that died before posting: take it over


def _claim_value() -> str:
    return f"{POSTING}{int(time.time())}"


async def _claim_first_post(plan: DailyPlan) -> Optional[str]:
    """Claim the day's first post. Returns the claim, or None when another save holds a live claim or has posted
    (then `plan` gets that post's channel / ts, if any, to edit it)."""
    coll = DailyPlan.get_motor_collection()
    claim = _claim_value()
    stale = f"{POSTING}{int(time.time()) - CLAIM_STALE_S}"
    free = {"$or": [{"slackTs": None}, {"slackTs": {"$gte": POSTING, "$lt": stale}}]}
    res = await coll.update_one({"_id": plan.id, **free}, {"$set": {"slackTs": claim}})
    if res.modified_count:
        return claim
    fresh = await coll.find_one({"_id": plan.id}, {"slackChannel": 1, "slackTs": 1}) or {}
    if not str(fresh.get("slackTs") or "").startswith(POSTING):
        plan.slackChannel, plan.slackTs = fresh.get("slackChannel"), fresh.get("slackTs")
    return None


async def _sync_slack(plan: DailyPlan, owner: UserExtra, done: Counts, rounds: dict) -> None:
    """Post or edit the day's standup message (only once the day is submitted); remember where it lives.

    The first post is claimed atomically: a save racing it skips posting (the winner's post stands and the next save
    edits it). Only the slack fields are written back, so no other save's fields are touched."""
    if not plan.submittedAt:
        return
    if str(plan.slackTs or "").startswith(POSTING):
        plan.slackTs = None
    claim = None
    if not plan.slackTs:
        claim = await _claim_first_post(plan)
        if claim is None and not plan.slackTs:
            log.info("daily plan %s: first post in flight elsewhere; skipping", plan.id)
            return
    when = plan.date.date()
    monday = monday_of(when)
    weekly = await week_or_draft(owner.id, monday)
    days = await _week_days(owner.id, monday)
    held = await _interviews(owner.id, monday, when)
    target = (weekly.goal.interviewsSelf + weekly.goal.interviewsCaller) if weekly else 0
    posted = await sync_daily_plan_message(
        plan, owner, done=done, rounds=rounds, targets=day_targets(plan, weekly, when),
        items=day_items(weekly, plan, when), progress=goal_progress(weekly, days) if weekly else {},
        week_interviews=(sum(e["self"] + e["caller"] for e in held.values()), target),
        follow_ups=await follow_ups_summary(owner.id, when),
        profile_names={p["id"]: p["name"] for p in await owner_profiles(owner.id)},
    )
    coll = DailyPlan.get_motor_collection()
    if claim is not None and not posted:  # the post failed: free the claim for the next save
        await coll.update_one({"_id": plan.id, "slackTs": claim}, {"$set": {"slackTs": None}})
        return
    if posted and posted != (plan.slackChannel, plan.slackTs):
        plan.slackChannel, plan.slackTs = posted
        await coll.update_one({"_id": plan.id}, {"$set": {"slackChannel": posted[0], "slackTs": posted[1]}})


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
    plans = await _week_days(uid, monday)
    by_date = {p.date.date(): p for p in plans}
    interviews = await _interviews(uid, monday, sunday)
    weekly = await week_or_draft(uid, monday)
    days = [monday + timedelta(days=n) for n in range(6)]
    if sunday in by_date:
        days.append(sunday)
    bidder = await asyncio.gather(*(plan_bidder.bidder_done(uid, d) for d in days))
    return {
        "weekStart": monday.isoformat(),
        "profiles": await owner_profiles(uid),
        "days": [
            _day_out(uid, d, by_date.get(d), interviews.get(d, empty_day()), weekly, plans, n)
            for d, n in zip(days, bidder)
        ],
    }


@router.get("/{day}")
async def get_day(
    day: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
) -> dict:
    when = parse_ymd(day, "date")
    uid = await read_subject(user, userId)
    return await _view(uid, when, rounds=uid == user.id or user.role == "admin")


async def _view(uid, when: date, rounds: bool = True) -> dict:
    """`rounds`: list the day's interviews. They are the owner's own (and an admin's to read): a teammate reading
    the plan gets none."""
    monday = monday_of(when)
    plans = await _week_days(uid, monday)
    plan = next((p for p in plans if p.date.date() == when), None)
    auto_day = (await _interviews(uid, when, when)).get(when, empty_day())
    by_profile = await plan_bidder.bidder_done_by_profile(uid, when)
    out = _day_out(uid, when, plan, auto_day, await week_or_draft(uid, monday), plans,
                   sum(by_profile.values()), await owner_profiles(uid))
    out.update(_bids_tables(out, by_profile, when >= plan_bidder.RELEASE_DAY))
    out["followUps"] = await follow_ups_summary(uid, when)  # read-only, never stored on the plan
    out["todayInterviews"] = today = await day_interviews(uid, when, _now()) if rounds else []
    out["interviewsMarked"] = {"marked": sum(r["state"] not in ("upcoming", "now", "unmarked") for r in today),
                               "total": len(today)}
    return await _with_tracked(out, uid, when)


def _bids_tables(out: dict, bidder_by_profile: dict, live: bool = True) -> dict:
    """The day view's `bidsByRegion` (US, EU, Latam; only the regions with a live profile, a target or a bid) and
    `bidsTotals`, from its `done`, `targets` and `profiles`. A bidder's bids are in the region of the profile it bids
    for; a target of 0 is none (null). `live`: the bidder total is the count the regions are cut from, so the two
    always agree (the saved `done.bidsBidder` can lag a write behind); a day before the release keeps its typed one."""
    done, targets = out["done"], out["targets"]
    bidder_total = sum(bidder_by_profile.values()) if live else done["bidsBidder"]
    region_of = {p["id"]: p["region"] for p in out["profiles"]}
    live = {p["region"] for p in out["profiles"] if not p["archived"]}
    rows = []
    for region, field in REGION_FIELD.items():
        bidder = sum(n for pid, n in bidder_by_profile.items() if region_of.get(str(pid)) == region)
        row = {"region": region, "self": done[field], "selfTarget": targets[field] or None, "bidder": bidder,
               "total": done[field] + bidder}
        if region in live or row["selfTarget"] or row["total"]:
            rows.append(row)
    return {"bidsByRegion": rows, "bidsTotals": {
        "self": done["bidsSelf"], "selfTarget": targets["bidsSelf"] or None,
        "bidder": bidder_total, "bidderTarget": targets["bidsBidder"] or None,
        "total": done["bidsSelf"] + bidder_total,
        "target": (targets["bidsSelf"] + targets["bidsBidder"]) or None}}


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
    submit: bool = True,
) -> dict:
    """Save the day's update. `submit=true` (default) submits it and posts it to Slack; `submit=false` is a draft
    save of item results / focus only (a submitted day's post is re-rendered)."""
    require_writer(user)
    when = parse_ymd(day, "date")
    body = parse_body(DayIn if submit else ProgressIn, payload)
    if when > _today():
        raise HTTPException(status_code=422, detail="A day can be followed up from that day on")
    if submit:
        await save_day(user, when, body)
    else:
        await save_progress(user, when, body)
    return await _view(user.id, when)


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
