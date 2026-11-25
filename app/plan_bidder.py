"""Bidder bids in the Daily Plan follow Bid Track: approved + pending bids of the owner's bidders, per report day."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from app.bids.windows import schedule_from_settings
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.profile_metrics import bid_report_day

log = logging.getLogger(__name__)

RELEASE_DAY = date(2026, 11, 16)  # days before this keep their typed numbers


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def bids_by_day(bidder_ids: list, start: date, end: date, with_status: bool = False) -> Counter:
    """(bidder id, report day) -> approved and pending bids of these bidders on the Bid Track days in [start, end].
    `with_status`: keyed (bidder id, report day, status) instead."""
    out: Counter = Counter()
    if not bidder_ids:
        return out
    sched = schedule_from_settings()
    # Report days don't align with UTC midnight: query a day wider on each side, then filter by report day.
    async for d in Bid.get_motor_collection().find(
        {"bidderId": {"$in": bidder_ids}, "kind": {"$ne": "self"}, "status": {"$in": ["approved", "pending"]},
         "firstAt": {"$gte": _midnight(start) - timedelta(days=1), "$lt": _midnight(end) + timedelta(days=2)}},
        {"firstAt": 1, "bidderId": 1, "status": 1},
    ):
        day = bid_report_day(d["firstAt"], sched)
        if start <= day <= end:
            out[(d["bidderId"], day, d["status"]) if with_status else (d["bidderId"], day)] += 1
    return out


async def bidder_split_by_profile(owner_id, day: date) -> dict:
    """The day's bids of the owner's bidders (archived ones too) per profile they bid for (None: on no profile), as
    Counter({"approved": n, "pending": n})."""
    profile_of = {b.id: b.profileId for b in await Bidder.find({"createdBy": owner_id}).to_list()}
    out: dict = {}
    for (bidder_id, _day, status), n in (await bids_by_day(list(profile_of), day, day, with_status=True)).items():
        out.setdefault(profile_of[bidder_id], Counter())[status] += n
    return out


async def bidder_done_by_profile(owner_id, day: date) -> Counter:
    """The day's bids of the owner's bidders (archived ones too) per profile they bid for (None: on no profile)."""
    return Counter({pid: sum(c.values()) for pid, c in (await bidder_split_by_profile(owner_id, day)).items()})


async def bidder_done(owner_id, day: date) -> int:
    return sum((await bidder_done_by_profile(owner_id, day)).values())


async def write_through_bidder(owner_id, day: date) -> None:
    """Set the day's done.bidsBidder (plan created if missing, loggedAt untouched); re-render a submitted, posted plan."""
    try:
        if day < RELEASE_DAY:
            return
        from pymongo.errors import DuplicateKeyError

        from app.alerts import app_week_key

        n = await bidder_done(owner_id, day)
        coll = DailyPlan.get_motor_collection()
        key = {"userId": owner_id, "date": _midnight(day)}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        year, week_number, _key = app_week_key(_midnight(day))
        update = {"$set": {"done.bidsBidder": n, "updatedAt": now},
                  "$setOnInsert": {"year": year, "weekNumber": week_number, "createdAt": now, "loggedAt": None}}
        try:
            await coll.update_one(key, update, upsert=True)
        except DuplicateKeyError:  # a concurrent first write created the plan between the upsert's find and insert
            await coll.update_one(key, update)
        from app.models import UserExtra
        from app.self_bids import resync_slack

        owner = await UserExtra.get(owner_id)
        if owner is not None:
            await resync_slack(owner, day, need_submitted=True)
    except Exception:
        log.exception("plan bidder: writing %s's bidder bids for %s failed", owner_id, day)
