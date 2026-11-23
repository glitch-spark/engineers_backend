"""The day save shared by the app (PUT /daily-plans/{day}) and the Slack standup form."""

import logging
import time
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError

from app import plan_bidder, self_bids
from app.alerts import app_week_key
from beanie import PydanticObjectId

from app.models import Account, UserExtra
from app.models.daily_plan import DailyPlan
from app.models.plan_common import BidsIn, Counts, CountsIn, DoneCounts, ItemResult, LogEntry, bids_of, clean_answer, monday_of, result_is_done
from app.plan_auto import (
    day_items, day_targets, effective_daily_done, empty_day, week_or_draft, weekly_plans_for,
)
from app.plan_profiles import derive_self_bids, owner_profiles, profile_regions

log = logging.getLogger(__name__)


class ItemResultIn(BaseModel):
    done: bool = False
    value: Optional[float] = Field(default=None, ge=0)
    entries: Optional[list[LogEntry]] = None  # log items: None = not sent (keep stored), [] = clear
    text: Optional[str] = None  # text answer / choice note: not sent = keep stored, null or "" = clear
    choice: Optional[str] = None  # choice items: same; must be one of the item's options

    @field_validator("text")
    @classmethod
    def _answer(cls, v: Optional[str]) -> Optional[str]:
        return clean_answer(v)

    @field_validator("choice")
    @classmethod
    def _choice(cls, v: Optional[str]) -> Optional[str]:
        return (v or "").strip() or None


class DayIn(BaseModel):
    """A day's update: item results, focus goals, self bids typed per profile (bidder bids come from Bid Track from
    plan_bidder.RELEASE_DAY on), notes and an optional own target."""

    results: dict[str, ItemResultIn] = {}
    focus: list[str] = []
    done: BidsIn = Field(default_factory=BidsIn)
    notes: str = Field(default="", max_length=2000)
    targetOverride: Optional[CountsIn] = None


class ProgressIn(BaseModel):
    """A draft save: only the sent item results (and focus, when sent) change."""

    results: dict[str, ItemResultIn] = {}
    focus: Optional[list[str]] = None


def _sent_done(r: "ItemResultIn") -> Optional[bool]:
    return r.done if "done" in r.model_fields_set else None


def result_done(kind: str, value: Optional[float], sent_done: Optional[bool], target: Optional[float],
                old: Optional[ItemResult]) -> bool:
    """`sent_done` None = not sent (Slack form): keep the stored done. Checks follow it; counts / hours are done when
    ticked, or at their frozen target (any value without one)."""
    def reached(v: Optional[float]) -> bool:
        return v is not None and (v >= target if target else v > 0)

    # not sent: keep a stored done only when the stored number did not already explain it (else it would stick)
    base = sent_done if sent_done is not None else bool(old and old.done and (kind == "check" or not reached(old.value)))
    if kind == "check":
        return bool(base)
    return base or reached(value)


def typed_answer(item, r: "ItemResultIn", prev: Optional[ItemResult]) -> ItemResult:
    """A text / choice item's result: sent fields replace the stored ones, unsent are kept. A sent choice must be one
    of the item's current options (a stored one that no longer is stays while not re-sent)."""
    sent = r.model_fields_set
    text = r.text if "text" in sent else prev.text if prev else None
    choice = r.choice if "choice" in sent else prev.choice if prev else None
    if item.kind == "text":
        choice = None
    elif "choice" in sent and choice is not None and choice not in item.options:
        raise HTTPException(status_code=422, detail="Unknown option")
    out = ItemResult(text=text, choice=choice)
    out.done = result_is_done(item.kind, out, None)
    return out


async def _clean_entries(user_id, sent: list[LogEntry]) -> list[LogEntry]:
    """Own profiles are kept; another user's profile is a 422; an id of no account (deleted, or not an id) is dropped
    to profileId None so the entry still saves."""
    own = {p["id"] for p in await owner_profiles(user_id)}
    foreign = [PydanticObjectId(e.profileId) for e in sent
               if e.profileId and e.profileId not in own and PydanticObjectId.is_valid(e.profileId)]
    theirs = {str(a["_id"]) for a in await Account.get_motor_collection().find(
        {"_id": {"$in": foreign}}, {"_id": 1}).to_list(None)} if foreign else set()
    out = []
    for e in sent:
        if e.profileId in theirs:
            raise HTTPException(status_code=422, detail="Unknown profile")
        out.append(e if e.profileId in own else LogEntry(note=e.note))
    return out


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


async def save_day(user: UserExtra, when: date, body: DayIn, *, _retry: bool = True) -> DailyPlan:
    """Save the owner's day (any day, weekends too) as submitted, then post / edit the standup post.

    Two first saves at once (app and Slack form) race on the unique (userId, date) index: the loser saves again over
    the winner's document."""
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

    results = {}
    for iid, r in body.results.items():
        if iid not in items:
            continue
        kind = items[iid]["item"].kind
        if kind in ("text", "choice"):
            results[iid] = typed_answer(items[iid]["item"], r, old.get(iid))
            continue
        entries = []
        if kind == "log":
            entries = await _clean_entries(user.id, r.entries) if r.entries is not None else (
                old[iid].entries if iid in old else [])
        results[iid] = ItemResult(
            done=bool(entries) if kind == "log" else result_done(kind, r.value, _sent_done(r), frozen(iid), old.get(iid)),
            value=None if kind == "log" else r.value, target=None if kind == "log" else frozen(iid), entries=entries)
    plan.results = results
    plan.focus = [i for i in dict.fromkeys(body.focus) if i in items and items[i]["scope"] == "goal"]
    # Freeze the day's targets (a day saved before the week has a plan keeps deriving them).
    if body.targetOverride is not None:
        plan.targetOverride = Counts(**body.targetOverride.model_dump())
    elif weekly is not None:
        plan.targetOverride = day_targets(plan, weekly, when)
    if plan.targetOverride is not None:
        plan.goal = Counts(**bids_of(plan.targetOverride))  # deprecated copy of the frozen targets (old-shape readers)

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
        try:
            await plan.insert()
        except DuplicateKeyError:
            if not _retry:
                raise
            log.info("plan save: user=%s day=%s lost the first-save race, saving as an update", user.id, when)
            return await save_day(user, when, body, _retry=False)
    else:
        # Every field but the Slack post's: a racing save may have just posted, and _sync_slack owns those two.
        plan.updatedAt = now
        fields = plan.model_dump(exclude={"id", "revision_id", "slackChannel", "slackTs"})
        await DailyPlan.get_motor_collection().update_one({"_id": plan.id}, {"$set": fields})

    auto_day = (await daily_plans._interviews(user.id, when, when)).get(when, empty_day())
    await daily_plans._sync_slack(plan, user, effective_daily_done(plan, auto_day), auto_day["rounds"])
    log.info("plan save: user=%s day=%s results=%d ms=%d", user.id, when, len(plan.results),
             (time.monotonic() - started) * 1000)
    return plan


async def save_progress(user: UserExtra, when: date, body: ProgressIn) -> None:
    """Store ticks / values without submitting: no Slack post for an unsubmitted day, no frozen targets, and `done`,
    `notes` and `targetOverride` stay as they are. A submitted day's post is re-rendered."""
    started = time.monotonic()
    weekly = (await weekly_plans_for([user.id], monday_of(when))).get(user.id)
    if weekly is None:
        raise HTTPException(status_code=422, detail="Set this week's plan first")
    key = {"userId": user.id, "date": _midnight(when)}
    plan = await DailyPlan.find_one(key)
    items = {e["item"].id: e for e in day_items(weekly, plan, when)}
    unknown = [i for i in body.results if i not in items]
    focus = None
    if body.focus is not None:
        focus = list(dict.fromkeys(body.focus))
        unknown += [i for i in focus if i not in items or items[i]["scope"] != "goal"]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown items: {', '.join(unknown)}")

    old = plan.results if plan else {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fields: dict = {"updatedAt": now}
    for iid, r in body.results.items():
        prev = old.get(iid)
        frozen = prev.target if prev and prev.target is not None else items[iid]["item"].target
        kind = items[iid]["item"].kind
        if kind in ("text", "choice"):
            fields[f"results.{iid}"] = typed_answer(items[iid]["item"], r, prev).model_dump()
            continue
        if kind == "log":
            entries = await _clean_entries(user.id, r.entries) if r.entries is not None else (prev.entries if prev else [])
            fields[f"results.{iid}"] = ItemResult(done=bool(entries), entries=entries).model_dump()
            continue
        value = r.value if "value" in r.model_fields_set or prev is None else prev.value  # unsent keeps the amount
        fields[f"results.{iid}"] = ItemResult(
            done=result_done(kind, value, _sent_done(r), frozen, prev),
            value=value, target=prev.target if prev else None,
            entries=prev.entries if prev else []).model_dump()
    if focus is not None:
        fields["focus"] = focus
    year, week_number, _key = app_week_key(_midnight(when))
    update = {"$set": fields,
              "$setOnInsert": {"year": year, "weekNumber": week_number, "createdAt": now, "loggedAt": None}}
    coll = DailyPlan.get_motor_collection()
    try:
        await coll.update_one(key, update, upsert=True)
    except DuplicateKeyError:  # a concurrent first write created the day between the upsert's find and insert
        await coll.update_one(key, update)
    stored = await DailyPlan.find_one(key)
    if stored is not None and stored.submittedAt:
        await self_bids.resync_slack(user, when)
    log.info("plan progress: user=%s day=%s ids=%d ms=%d", user.id, when, len(body.results),
             (time.monotonic() - started) * 1000)
