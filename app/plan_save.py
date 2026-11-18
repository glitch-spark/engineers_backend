"""The day save shared by the app (PUT /daily-plans/{day}) and the Slack standup form."""

import logging
import time
from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from app import plan_bidder, self_bids
from app.alerts import app_week_key
from app.models import UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import BidsIn, Counts, CountsIn, DoneCounts, ItemResult, bids_of, monday_of
from app.plan_auto import day_items, day_targets, effective_daily_done, empty_day, week_or_draft
from app.plan_profiles import derive_self_bids, owner_profiles, profile_regions

log = logging.getLogger(__name__)


class ItemResultIn(BaseModel):
    done: bool = False
    value: Optional[float] = Field(default=None, ge=0)


class DayIn(BaseModel):
    """A day's update: item results, focus goals, self bids typed per profile (bidder bids come from Bid Track from
    plan_bidder.RELEASE_DAY on), notes and an optional own target."""

    results: dict[str, ItemResultIn] = {}
    focus: list[str] = []
    done: BidsIn = Field(default_factory=BidsIn)
    notes: str = Field(default="", max_length=2000)
    targetOverride: Optional[CountsIn] = None


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def save_day(user: UserExtra, when: date, body: DayIn) -> DailyPlan:
    """Save the owner's day (any day, weekends too) as submitted, then post / edit the standup post."""
    from app.routers import daily_plans  # holds _sync_slack / _interviews (self_bids and tests use them there)

    started = time.monotonic()
    plan = await DailyPlan.find_one({"userId": user.id, "date": _midnight(when)})
    weekly = await week_or_draft(user.id, monday_of(when))
    if plan is None:
        year, week_number, _key = app_week_key(_midnight(when))
        plan = DailyPlan(userId=user.id, date=_midnight(when), year=year, weekNumber=week_number)

    # Results only for the day's items; each keeps the target it was first saved against (frozen).
    items = {e["item"].id: e for e in day_items(weekly, plan, when)}
    old = plan.results

    def frozen(iid: str) -> Optional[float]:
        return old[iid].target if iid in old and old[iid].target is not None else items[iid]["item"].target

    plan.results = {
        iid: ItemResult(done=r.done, value=r.value, target=frozen(iid))
        for iid, r in body.results.items() if iid in items
    }
    plan.focus = [i for i in dict.fromkeys(body.focus) if i in items and items[i]["scope"] == "goal"]
    # Freeze the day's targets (a day saved before the week has a plan keeps deriving them).
    if body.targetOverride is not None:
        plan.targetOverride = Counts(**body.targetOverride.model_dump())
    elif weekly is not None:
        plan.targetOverride = day_targets(plan, weekly, when)
    if plan.targetOverride is not None:
        plan.goal = Counts(**bids_of(plan.targetOverride))  # deprecated copy, still read by the standup post

    if when >= plan_bidder.RELEASE_DAY:
        bidder = await plan_bidder.bidder_done(user.id, when)
    elif "bidsBidder" in body.done.model_fields_set:
        bidder = body.done.bidsBidder  # days before the release keep their typed value
    else:
        bidder = plan.done.bidsBidder

    profiles = await owner_profiles(user.id)
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
    plan.done.bidsBidder = bidder
    plan.notes = body.notes
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if plan.loggedAt is None:
        plan.loggedAt = now
    if plan.submittedAt is None:
        plan.submittedAt = now
    if plan.id is None:
        await plan.insert()
    else:
        await plan.save()

    auto_day = (await daily_plans._interviews(user.id, when, when)).get(when, empty_day())
    await daily_plans._sync_slack(plan, user, effective_daily_done(plan, auto_day), auto_day["rounds"])
    log.info("plan save: user=%s day=%s results=%d ms=%d", user.id, when, len(plan.results),
             (time.monotonic() - started) * 1000)
    return plan
