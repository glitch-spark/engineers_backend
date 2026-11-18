"""Self bids tracked by the extension (spec 2026-11-15-self-bids-extension-design.md).

Pilot (SELF_BIDS_TRACKED_FROM unset): tracked self bids are shown next to the typed Daily Plan numbers and change
nothing. Switch (setting set): each (owner, profile) moves to tracked counts from its own first tracked report day on
or after the setting; that day's self bids for the profile = tracked + "other applies" typed (LinkedIn, Indeed,
another device), written into the Daily Plan day so every reader of the plan gets them. Other profiles stay typed."""
from __future__ import annotations

import logging

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.bids.windows import schedule_from_settings
from app.config import get_settings
from app.models.bid import Bid
from app.models.daily_plan import DailyPlan
from app.models.plan_common import DoneCounts, bids_of
from app.plan_profiles import derive_self_bids, owner_profiles
from app.profile_metrics import bid_report_day

log = logging.getLogger(__name__)


def tracked_from() -> Optional[date]:
    s = get_settings().SELF_BIDS_TRACKED_FROM
    return date.fromisoformat(s) if s else None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def tracked_counts(owner_id, start: date, end: date) -> dict[tuple[str, date], int]:
    """{(profile id, report day): self bids} of this owner over [start, end] (report days, as bidder bids)."""
    sched = schedule_from_settings()
    out: dict = defaultdict(int)
    async for d in Bid.get_motor_collection().find(
        {"kind": "self", "ownerId": owner_id,
         "firstAt": {"$gte": _midnight(start) - timedelta(days=1), "$lt": _midnight(end) + timedelta(days=2)}},
        {"accountId": 1, "firstAt": 1},
    ):
        day = bid_report_day(d["firstAt"], sched)
        if start <= day <= end and d.get("accountId"):
            out[(str(d["accountId"]), day)] += 1
    return dict(out)


async def tracked_for_day(owner_id, day: date) -> dict[str, int]:
    return {pid: n for (pid, d), n in (await tracked_counts(owner_id, day, day)).items()}


async def switched_profiles(owner_id, day: date) -> set[str]:
    """The owner's profiles on tracked counts on `day`: the switch is on and the profile's first tracked report day on
    or after it is `day` or earlier. Empty during the pilot."""
    since = tracked_from()
    if since is None or day < since:
        return set()
    sched = schedule_from_settings()
    first: dict[str, date] = {}
    async for d in Bid.get_motor_collection().find(
        {"kind": "self", "ownerId": owner_id, "firstAt": {"$gte": _midnight(since) - timedelta(days=1)}},
        {"accountId": 1, "firstAt": 1},
    ):
        rd = bid_report_day(d["firstAt"], sched)
        pid = str(d.get("accountId") or "")
        if pid and rd >= since and (pid not in first or rd < first[pid]):
            first[pid] = rd
    return {pid for pid, f in first.items() if f <= day}


async def day_self_bids(owner_id, day: date, typed: dict[str, int], other: dict[str, int]) -> dict[str, int]:
    """The day's self bids per profile: typed for unswitched profiles; tracked + other for switched ones."""
    switched = await switched_profiles(owner_id, day)
    tracked = await tracked_for_day(owner_id, day) if switched else {}
    by = {pid: n for pid, n in typed.items() if pid not in switched and n}
    for pid in switched:
        n = tracked.get(pid, 0) + other.get(pid, 0)
        if n:
            by[pid] = n
    return by


async def write_through(owner_id, day: date) -> None:
    """After the switch: put the day's tracked self bids into its Daily Plan (created if the owner never filled one
    in), keeping typed profiles and "other applies". A no-op during the pilot or before a profile switched. Only the
    done fields are written (atomically), so a save of the day in between keeps its notes, lines and Slack post."""
    await _write(owner_id, day)


async def _write(owner_id, day: date) -> None:
    if not await switched_profiles(owner_id, day):
        return
    from pymongo.errors import DuplicateKeyError

    from app.alerts import app_week_key

    coll = DailyPlan.get_motor_collection()
    key = {"userId": owner_id, "date": _midnight(day)}
    existing = await coll.find_one(key, {"done": 1}) or {}
    done = DoneCounts(**(existing.get("done") or {}))
    other = dict(done.bidsSelfOtherByProfile or {})
    by = await day_self_bids(owner_id, day, dict(done.bidsSelfByProfile or {}), other)
    regions = {p["id"]: p["region"] for p in await owner_profiles(owner_id)}
    derived = derive_self_bids(by, {pid: regions.get(pid) for pid in by})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    year, week_number, _key = app_week_key(_midnight(day))
    update = {
        "$set": {**{f"done.{k}": v for k, v in derived.items()}, "done.bidsSelfByProfile": by,
                 "done.bidsSelfOtherByProfile": other, "updatedAt": now},
        "$setOnInsert": {"year": year, "weekNumber": week_number, "createdAt": now, "loggedAt": now},
    }
    try:
        await coll.update_one(key, update, upsert=True)
    except DuplicateKeyError:  # the day's plan was created between our read and write: update it
        await coll.update_one(key, update)
    await coll.update_one({**key, "loggedAt": None}, {"$set": {"loggedAt": now}})


async def resync_slack(owner, day: date, *, need_submitted: bool = False) -> None:
    """Update the day's Slack post (when it has one) to the plan's current numbers."""
    from app.plan_auto import effective_daily_done, empty_day
    from app.routers import daily_plans

    plan = await DailyPlan.find_one({"userId": owner.id, "date": _midnight(day)})
    if plan is None or not plan.slackTs or (need_submitted and not plan.submittedAt):
        return
    auto_day = (await daily_plans._interviews(owner.id, day, day)).get(day, empty_day())
    await daily_plans._sync_slack(plan, owner, effective_daily_done(plan, auto_day), auto_day["rounds"])


async def safe_write_through(owner, day: date) -> None:
    """For an upload: the write-through and, when the day was posted to Slack, the post updated to the new count.
    Never fails the upload (the bid is already recorded; the next upload or a save of the day writes it again)."""
    try:
        await _write(owner.id, day)
        await resync_slack(owner, day)
    except Exception:
        log.exception("self bids: writing %s's tracked count for %s failed", owner.id, day)
